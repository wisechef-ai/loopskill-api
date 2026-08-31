"""Issue #277 Fix A — the shared external-install resolver (extracted).

Split out of ``bundle_external.py`` when the resolver pushed that module past
the 600-line god-module gate (test_w0_2_pyfile_size_discipline) — the gate is
correct, splitting beats waiving. Issue #281: the REST route
``skill_routes.install_external_skill`` now consumes
``resolve_external_install_full`` too, so this is THE one code path for every
transport (REST, MCP, bundle/well-known readers go through the legacy thin
shim below).
"""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.services.federation import (
    INTERNAL_SOURCE,
    ExternalSkill,
    InstallPath,
    route_install,
)
from app.services.federation_adapters import get_adapter
from app.services.federation_install import get_origin_fetcher
from app.services.federation_live import LIVE_FETCH
from app.services.federation_scan import QUALITY_AS_IS, scan_external_body

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Cross-module seam: attribution text is owned by the legacy bundle_external
# module (single source of truth); the resolver imports it lazily to avoid a
# circular import (bundle_external does not import this module).
from app.services.bundle_external import (  # noqa: E402
    ExternalInstallResolution,
    ExternalSourceUnavailable,
    build_attribution,
)

__all__ = [
    "ExternalInstallResolution",
    "ExternalSourceUnavailable",
    "known_external_sources",
    "resolve_external_install_full",
    "validate_external_slug",
]


# ─────────────────────────────────────────────────────────────────────────────


def known_external_sources() -> frozenset[str]:
    """The EXACT set of live federated source ids (adapters + GitHub taps).

    Used by the MCP federated-ref parser: the left token of a ``source--rest``
    or ``source:rest`` ref must be an exact member of this set — never a
    prefix match, so ``github`` can never swallow ``github-enterprise--x``.
    Config-driven (config/federation_sources.yaml) so a newly-federated tap is
    routable with zero code change.
    """
    from app.services.federation_sources_config import adapter_source_ids, github_tap_rows

    ids = set(adapter_source_ids())
    ids.update(str(r["source_id"]) for r in github_tap_rows())
    return frozenset(ids)


_EXTERNAL_SLUG_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789._/-"


def validate_external_slug(slug: str) -> bool:
    """Charset + traversal guard for a slug about to hit ``adapter.resolve``.

    Several adapters convert ``--`` back into ``/`` before an upstream lookup,
    so a hostile slug is a path-confusion vector even behind the SSRF guards.
    Rejects: empty, >200 chars, any char outside [a-z0-9._/-], NUL/control
    bytes, any ``..`` sequence, and leading ``/`` or ``.``.
    """
    if not slug or len(slug) > 200:
        return False
    if slug.lower() != slug:
        # Federated slugs are lowercase by construction across every tap.
        return False
    if any(c not in _EXTERNAL_SLUG_SAFE for c in slug):
        return False
    if ".." in slug or slug[0] in "/." or slug[-1] in "/-":
        return False
    # codex review (#277, finding 7): reject alias/empty-destination shapes.
    # `--` alone -> empty adapter identifier; trailing `--` -> empty install
    # leaf (command would target ~/.claude/skills/SKILL.md); `//` and `./`
    # normalize to distinct paths upstream -> same artifact, two identities.
    if slug == "--" or slug.endswith("--"):
        return False
    segments = [seg for part in slug.split("/") for seg in part.split("--")]
    if any(seg in ("", ".") for seg in segments):
        # empty segments (from `//`, trailing `/`, bare `--`) and literal `.`
        # segments (from `a/./b`) both normalize upstream into ALIAS paths —
        # one artifact must never have two installable identities here.
        return False
    return True


def _safe_origin_url(url: str | None) -> str | None:
    """Only ever hand callers an http(s) origin_url — an upstream-controlled
    ``javascript:`` / ``data:`` scheme rendered as a link is client XSS.
    """
    if not url:
        return None
    try:
        scheme = urlparse(url).scheme.lower()
    # Rationale: an unparseable upstream URL is dropped, never propagated.
    except Exception:  # noqa: BLE001
        return None
    return url if scheme in ("http", "https") else None


def _safe_endpoint_url(url: str | None) -> str | None:
    """REGISTER_MCP endpoint guard (codex review, #277): require http(s) WITH a
    hostname AND no control characters. An endpoint carrying a newline can
    break out of the YAML/JSON config block federation_mcp builds by string
    interpolation (``url: https://good/\ncommand: pwn``) — configuration
    injection, not just a bad link.
    """
    safe = _safe_origin_url(url)
    if safe is None:
        return None
    if any(ord(c) < 0x20 for c in safe):
        return None
    try:
        host = urlparse(safe).hostname
    # Rationale: unparseable = unsafe; never propagate.
    except Exception:  # noqa: BLE001
        return None
    return safe if host else None


def _sanitize_payload_urls(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the http(s)-only guard to every URL field in a resolver payload,
    and rebuild install_command with shell-quoted components when present.

    codex review (#277, finding 1): when raw_url FAILS validation the
    upstream-derived install_command must be DROPPED, not kept — the original
    command was built from the unsanitized URL and is an injection vector.
    Never retain an upstream command we could not rebuild ourselves.
    """
    for key in ("origin_url", "raw_url"):
        if key in payload:
            payload[key] = _safe_origin_url(payload.get(key))
    if "endpoint" in payload:
        payload["endpoint"] = _safe_endpoint_url(payload.get("endpoint"))
    cmd = payload.get("install_command")
    raw = payload.get("raw_url")
    if payload.get("install_path") == InstallPath.FETCH_ORIGIN.value:
        if not (cmd and raw):
            payload.pop("install_command", None)
        else:
            # Rebuild rather than trust: leaf derives from the slug (already
            # charset-validated), raw_url just passed the scheme guard — quote both.
            leaf = str(payload.get("slug", "skill")).rsplit("--", 1)[-1]
            payload["install_command"] = (
                f"mkdir -p ~/.claude/skills/{shlex.quote(leaf)} && "
                f"curl -fsSL {shlex.quote(raw)} -o ~/.claude/skills/{shlex.quote(leaf)}/SKILL.md"
            )
    elif cmd and any(ord(c) < 0x20 for c in str(cmd)):
        # Non-fetch paths (REGISTER_MCP): never pass through a command
        # containing control characters.
        payload.pop("install_command", None)
    return payload


def resolve_external_install_full(
    db: "Session",
    source: str,
    slug: str,
    *,
    allow_live_resolve: bool = True,
) -> ExternalInstallResolution:
    """Typed resolve consumed by the MCP federated branch and (since #281) the
    REST route ``skill_routes.install_external_skill``.

    Cache-first (federation_index_cache first_page — what the reindex cron
    wrote), then a live ``adapter.resolve`` fallback when
    ``allow_live_resolve`` permits. The MCP branch passes False for github-*
    sources: MCP callers bypass the anonymous IP limiter, and a storm of bogus
    refs must not be able to drain the shared 60/hr GitHub budget (the REST
    route keeps live fallback — it sits behind the per-IP limiter).

    Returns an :class:`ExternalInstallResolution`; ``payload`` URLs are always
    scheme-guarded and ``install_command`` shell-quoted.
    """
    from app.services import federation_cache as fcache

    if source == INTERNAL_SOURCE or not validate_external_slug(slug):
        return ExternalInstallResolution(kind="not_found", payload=None)

    adapter = get_adapter(source, fetch=LIVE_FETCH.get(source))
    if adapter is None:
        return ExternalInstallResolution(kind="not_found", payload=None)

    ext: ExternalSkill | None = None
    for row in fcache.read_first_page(db, source):
        if isinstance(row, dict) and row.get("slug") == slug:
            ext = ExternalSkill.from_dict(row)
            break

    if ext is None:
        if not allow_live_resolve:
            return ExternalInstallResolution(kind="not_found", payload=None)
        try:
            ext = adapter.resolve(slug)
        # Rationale: transport decides the status — REST 503s, MCP not_founds.
        except Exception as exc:  # noqa: BLE001
            logger.warning("external resolve failed: %s/%s", source, slug, exc_info=True)
            raise ExternalSourceUnavailable(str(exc)) from exc
    if ext is None:
        return ExternalInstallResolution(kind="not_found", payload=None)

    decision = route_install(ext)
    if not decision.allowed:
        payload = _sanitize_payload_urls(
            {
                "slug": ext.slug,
                "source": ext.source,
                "install_path": ext.install_path.value,
                "installed": False,
                "reason": decision.reason,
                "license": ext.license,
                "origin_url": ext.origin_url,
                "namespace": "external",
                "quality": QUALITY_AS_IS,
                "agent_instructions": (
                    "LoopSkill does not redistribute this skill (license unknown or "
                    "restricted). To install it, fetch the skill definition yourself "
                    f"directly from its origin page: {ext.origin_url} — locate the "
                    "SKILL.md (or equivalent) there and save it into your agent's "
                    "skills directory. Attribute the original author; respect the "
                    "origin's license and terms."
                ),
            }
        )
        return ExternalInstallResolution(kind="deep_link", payload=payload, skill=ext)

    if ext.install_path == InstallPath.REGISTER_MCP:
        from app.services.federation_mcp import build_mcp_server_config

        if _safe_endpoint_url(ext.origin_url) is None:
            # codex review (#277, finding 4): an endpoint that fails the
            # strict http(s)+host+no-controls guard must never reach the
            # config builder — build_mcp_server_config interpolates the URL
            # into YAML/JSON client configs, and an embedded newline is
            # configuration injection. Honest wiring_missing, not a config.
            return ExternalInstallResolution(
                kind="wiring_missing",
                payload={
                    "reason": (
                        f"register-mcp skill '{slug}' has no registrable "
                        "MCP endpoint (invalid or unsafe: not http(s), no "
                        "hostname, or control chars)"
                    ),
                    "install_path": ext.install_path.value,
                    "origin_url": _safe_origin_url(ext.origin_url),
                    "license": ext.license,
                },
                skill=ext,
            )
        try:
            cfg = build_mcp_server_config(ext)
        except ValueError:
            detail = {
                "reason": (
                    f"register-mcp skill '{slug}' has no registrable "
                    "MCP endpoint (origin_url is not a server URL)"
                ),
                "install_path": ext.install_path.value,
                "origin_url": _safe_origin_url(ext.origin_url),
                "license": ext.license,
            }
            return ExternalInstallResolution(kind="wiring_missing", payload=detail, skill=ext)
        payload = _sanitize_payload_urls(
            {
                "slug": ext.slug,
                "source": ext.source,
                "install_path": ext.install_path.value,
                "license": ext.license,
                "origin_url": ext.origin_url,
                "namespace": "external",
                "quality": QUALITY_AS_IS,
                "server_key": cfg["server_key"],
                "endpoint": cfg["endpoint"],
                "mcp_config": cfg["mcp_config"],
                "hermes_yaml": cfg["hermes_yaml"],
                "claude_desktop_json": cfg["claude_desktop_json"],
                "install_command": cfg["install_command"],
            }
        )
        return ExternalInstallResolution(kind="register_mcp", payload=payload, skill=ext)

    if ext.install_path == InstallPath.FETCH_ORIGIN:
        fetcher = get_origin_fetcher(source)
        if fetcher is None:
            detail = {
                "reason": f"fetch-origin install not yet wired for source '{source}'",
                "install_path": ext.install_path.value,
                "origin_url": _safe_origin_url(ext.origin_url),
                "license": ext.license,
            }
            return ExternalInstallResolution(kind="wiring_missing", payload=detail, skill=ext)
        fetch_row = {"slug": ext.slug, "origin_url": ext.origin_url, "source": ext.source}
        try:
            got = fetcher(slug, row=fetch_row)
        except TypeError:
            got = fetcher(slug)
        if got is None:
            return ExternalInstallResolution(kind="not_found", payload=None, skill=ext)
        raw_url, content = got
        verdict = scan_external_body(content)
        leaf = slug.rsplit("--", 1)[-1]
        payload = _sanitize_payload_urls(
            {
                "slug": ext.slug,
                "source": ext.source,
                "install_path": ext.install_path.value,
                "installed": True,
                "license": ext.license,
                "attribution": build_attribution(ext),
                "origin_url": ext.origin_url,
                "raw_url": raw_url,
                "content": content,
                "namespace": "external",
                "quality": QUALITY_AS_IS,
                "scan_status": verdict.badge,
                "scannable": verdict.scannable,
                "scan_findings": verdict.findings,
                "scan_warnings": verdict.warnings,
                "install_command": (
                    f"mkdir -p ~/.claude/skills/{leaf} && "
                    f"curl -fsSL {raw_url} -o ~/.claude/skills/{leaf}/SKILL.md"
                ),
            }
        )
        return ExternalInstallResolution(
            kind="fetch_origin", payload=payload, skill=ext, scan_verdict=verdict
        )

    # Any other allowed path has no executable body — honest deep-link-shaped
    # answer with an explanatory note (mirrors the REST fall-through).
    payload = _sanitize_payload_urls(
        {
            "slug": ext.slug,
            "source": ext.source,
            "install_path": ext.install_path.value,
            "license": ext.license,
            "origin_url": ext.origin_url,
            "namespace": "external",
            "quality": QUALITY_AS_IS,
            "attribution": "unattributed",
            "note": "This install path is not yet executable here; use the origin link.",
        }
    )
    return ExternalInstallResolution(kind="deep_link", payload=payload, skill=ext)
