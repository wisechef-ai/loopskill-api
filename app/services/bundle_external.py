"""federation_0604 Unit 2 — cookbooks hold external (federated) skills.

This module is the single seam that lets a Bundle hold a federated skill
(lobehub, clawhub, skills-sh, hermes-hub, browse-sh, well-known) and hand it
to an agent as ONE link — exactly like an internal skill — WITHOUT ever
rehosting external content.

Two responsibilities, kept here so the logic lives in ONE place (no drift
between the federation route and the cookbook route — the no-redundant-concepts
rule):

  1. ``materialize_external_skill`` — turn an external skill into a thin,
     PRIVATE ``Skill`` row so the existing ``cookbook_skills.skill_id`` FK and
     every downstream cookbook feature (install / manifest / sync / share
     token / handoff) work unchanged. The row is a POINTER, not a content
     snapshot: the re-resolution descriptor lives in ``external_resources``.

  2. ``resolve_external_install`` — the shared resolver that fetches the real
     SKILL.md from origin at install time (never rehosted), reusing the same
     federation adapters + origin fetchers the ``/skills/external/.../install``
     route uses. Bundle single-install and the federation route both call
     this, so the install contract can never drift.

Isolation contract (enforced by callers + the catalog filter):
  - Materialized rows are ``is_public=False`` → invisible to the public catalog
    (every catalog query filters ``is_public == True``).
  - They are reachable ONLY through cookbook membership (authz cookbook-scope
    clause) — same trust boundary as a private tailored fork.
  - ``skill_variant="external"`` + ``tier="external"`` tag them for the install
    router and the web viz badge.

Slug convention: ``ext:{source}:{external_slug}`` — deterministic + unique, so
materialize is idempotent (the same external skill maps to exactly one row,
shareable across cookbooks).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from app.services.federation import (
    INTERNAL_SOURCE,
    ExternalSkill,
    InstallPath,
    route_install,
)
from app.services.federation_adapters import get_adapter
from app.services.federation_install import get_origin_fetcher
from app.services.federation_live import LIVE_FETCH
from app.services.federation_scan import (
    BADGE_PENDING,
    QUALITY_AS_IS,
    normalize_badge,
    scan_external_body,
    scan_on_add,
)

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from app.models import Skill

logger = logging.getLogger(__name__)

# Slug namespace for materialized external skills. The ``ext:`` prefix is the
# tripwire every isolation assertion keys on — nothing else in the catalog uses
# it, and the public-catalog filter never returns these rows (is_public=False).
EXTERNAL_SLUG_PREFIX = "ext"
EXTERNAL_TIER = "external"
EXTERNAL_VARIANT = "external"


def external_slug(source: str, slug: str) -> str:
    """Deterministic catalog slug for a federated skill."""
    return f"{EXTERNAL_SLUG_PREFIX}:{source}:{slug}"


def is_external_skill(skill: "Skill | None") -> bool:
    """True iff this Skill row is a materialized federation pointer."""
    if skill is None:
        return False
    return getattr(skill, "skill_variant", None) == EXTERNAL_VARIANT or str(
        getattr(skill, "slug", "")
    ).startswith(EXTERNAL_SLUG_PREFIX + ":")


def known_external_source(source: str) -> bool:
    """True iff ``source`` is a wired federation source (not the internal one)."""
    if source == INTERNAL_SOURCE:
        return False
    return get_adapter(source, fetch=LIVE_FETCH.get(source)) is not None


def _resolve_external(source: str, slug: str) -> ExternalSkill | None:
    """Resolve an external skill via its source adapter.

    Isolated as a module-level seam so tests can stub the network. Returns the
    ``ExternalSkill`` descriptor or ``None`` (unknown source / unresolvable
    slug / source outage — all collapse to None; the caller decides the HTTP
    status).
    """
    if source == INTERNAL_SOURCE:
        return None
    adapter = get_adapter(source, fetch=LIVE_FETCH.get(source))
    if adapter is None:
        return None
    try:
        return adapter.resolve(slug)
    # Rationale: a source outage / parse error must degrade to "unresolvable",
    # never crash the bundle-add request.
    except Exception:  # noqa: BLE001
        logger.warning("external resolve failed: %s/%s", source, slug, exc_info=True)
        return None


def materialize_external_skill(db: "Session", source: str, slug: str) -> "Skill | None":
    """Materialize a federated skill as a thin, PRIVATE ``Skill`` row.

    Idempotent: the same (source, slug) maps to one row (slug
    ``ext:{source}:{slug}``). Returns the existing row if already materialized,
    a freshly-added (un-committed, flushed) row on first sight, or ``None`` if
    the skill cannot be resolved from its origin.

    The row is a POINTER: ``external_resources`` carries the re-resolution
    descriptor so install can fetch the body from origin. No SKILL.md content
    is stored here — federation never rehosts.
    """
    from app.models import Skill

    cat_slug = external_slug(source, slug)
    existing = db.query(Skill).filter(Skill.slug == cat_slug).first()
    if existing is not None:
        return existing

    ext = _resolve_external(source, slug)
    if ext is None:
        return None

    # spotify_0608 Phase C — scan-on-add trust badge. Fetch-origin sources get
    # the real 10-pattern scan run over the origin body ONCE here; the verdict
    # is cached in the descriptor so reads never re-scan. Deep-link / mcp /
    # non-redistributable sources never fetch a body and stay honestly
    # ``unscanned`` (the scan_on_add decision tree owns that distinction).
    verdict = scan_on_add(ext, get_origin_fetcher(source), slug)

    descriptor: dict[str, Any] = {
        "federation_source": source,
        "external_slug": slug,
        "install_path": ext.install_path.value,
        "origin_url": ext.origin_url,
        "redistributable": ext.redistributable,
        "scan_status": verdict.badge,
        "scannable": verdict.scannable,
        "scan_findings": verdict.findings,
        "scan_warnings": verdict.warnings,
    }
    skill = Skill(
        id=uuid4(),
        slug=cat_slug,
        title=ext.title or slug,
        description=ext.description or None,
        license=ext.license,
        is_public=False,  # ISOLATION WALL: never in the public catalog
        is_archived=False,
        tier=EXTERNAL_TIER,
        skill_variant=EXTERNAL_VARIANT,
        original_source_url=ext.origin_url,
        external_resources=descriptor,
    )
    db.add(skill)
    db.flush()
    return skill


def resolve_external_install(source: str, slug: str) -> dict[str, Any] | None:
    """Resolve a federated skill's REAL SKILL.md from origin at install time.

    The single source of truth for "install one external skill" — shared by the
    cookbook single-install route and the public ``/skills/external/.../install``
    route, so the contract cannot drift.

    Returns a payload dict ({slug, source, license, origin_url, raw_url,
    content, install_command, ...}) on success, or ``None`` when:
      - the skill is unresolvable,
      - the install router blocks it (deep-link / non-redistributable license),
      - no origin fetcher is wired, or the origin fetch fails.

    NEVER rehosts: ``content`` is streamed live from origin, with license +
    attribution preserved.
    """
    ext = _resolve_external(source, slug)
    if ext is None:
        return None

    decision = route_install(ext)
    if not decision.allowed:
        # Deep-link / non-redistributable: never rehosted — caller hands back
        # the origin link instead of a body.
        return None

    # REGISTER_MCP — no SKILL.md body; return a paste-ready MCP client-config
    # block pointing at the server's endpoint. Shared by the bundle single-
    # install route and the public /skills/external/.../install route so the
    # contract cannot drift.
    if ext.install_path == InstallPath.REGISTER_MCP:
        from app.services.federation_mcp import build_mcp_server_config

        try:
            cfg = build_mcp_server_config(ext)
        except ValueError:
            # No registrable endpoint — caller surfaces the honest 409.
            return None
        return {
            "slug": ext.slug,
            "source": ext.source,
            "install_path": ext.install_path.value,
            "license": ext.license,
            "origin_url": ext.origin_url,
            "namespace": "external",
            "quality": QUALITY_AS_IS,
            "scan_status": "unscanned",  # remote server — no body to scan
            "scannable": False,
            "server_key": cfg["server_key"],
            "endpoint": cfg["endpoint"],
            "mcp_config": cfg["mcp_config"],
            "hermes_yaml": cfg["hermes_yaml"],
            "claude_desktop_json": cfg["claude_desktop_json"],
            "install_command": cfg["install_command"],
        }

    if ext.install_path != InstallPath.FETCH_ORIGIN:
        # any other non-fetch path has no file body to stream.
        return None

    fetcher: Callable[[str], tuple[str, str] | None] | None = get_origin_fetcher(source)
    if fetcher is None:
        return None
    got = fetcher(slug)
    if got is None:
        return None
    raw_url, content = got

    # spotify_0608 Phase C — the single-install path fetched the EXACT bytes the
    # agent will run, so this is the authoritative scan moment. Run the real
    # scanner over the body and surface the badge alongside the content (the
    # cached add-time verdict may be stale; this is ground truth at install).
    verdict = scan_external_body(content)

    leaf = slug.rsplit("--", 1)[-1]
    return {
        "slug": ext.slug,
        "source": ext.source,
        "install_path": ext.install_path.value,
        "license": ext.license,
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
            f"mkdir -p ~/.claude/skills/{leaf} && curl -fsSL {raw_url} -o ~/.claude/skills/{leaf}/SKILL.md"
        ),
    }


def install_descriptor_for(cookbook_id: str, skill: "Skill") -> dict[str, Any]:
    """Cheap per-skill descriptor for the BULK cookbook-install payload.

    ISOLATION WALL #2: bulk install must NOT fetch N origins. For an external
    skill we return a pointer + the cookbook-scoped single-install URL the agent
    calls to fetch the real body on demand. No origin call happens here.

    spotify_0608 Phase C: the trust badge rides along from the cached add-time
    verdict (``scan_status`` in the descriptor) — zero extra fetches. Legacy
    rows with no cached status normalize to ``unscanned`` (fail-honest).
    """
    desc = skill.external_resources or {}
    return {
        "slug": skill.slug,
        "external": True,
        "source": desc.get("federation_source"),
        "version": None,
        "tarball_url": None,  # external skills have no tarball
        "checksum_sha256": None,
        "install_url": f"/api/cookbooks/{cookbook_id}/skills/{skill.slug}/install",
        "install_path": desc.get("install_path"),
        "quality": QUALITY_AS_IS,
        "scan_status": normalize_badge(desc.get("scan_status")),
        "scannable": bool(desc.get("scannable", False)),
    }


def rescan_pending_external(db: "Session", skill: "Skill") -> str:
    """Opportunistically re-scan a materialized row stuck in ``pending``.

    A ``pending`` badge means the add-time origin fetch failed TRANSIENTLY
    (origin down / timeout) — distinct from an honest ``unscanned`` deep-link.
    Callers may invoke this on read to upgrade a recovered origin to
    clean/flagged. No-op (returns the current badge) for any non-pending row, so
    it is safe to call unconditionally. Commits only when the badge changes.
    """
    desc = dict(skill.external_resources or {})
    current = normalize_badge(desc.get("scan_status"))
    if current != BADGE_PENDING:
        return current

    pair = descriptor_source_slug(skill)
    if pair is None:
        return current
    source, ext_slug = pair
    ext = _resolve_external(source, ext_slug)
    if ext is None:
        return current

    verdict = scan_on_add(ext, get_origin_fetcher(source), ext_slug)
    if verdict.badge == current:
        return current
    desc["scan_status"] = verdict.badge
    desc["scannable"] = verdict.scannable
    desc["scan_findings"] = verdict.findings
    desc["scan_warnings"] = verdict.warnings
    skill.external_resources = desc
    db.add(skill)
    db.commit()
    return verdict.badge


def descriptor_source_slug(skill: "Skill") -> tuple[str, str] | None:
    """Recover (source, external_slug) from a materialized row's descriptor.

    Falls back to parsing the ``ext:{source}:{slug}`` catalog slug if the
    descriptor is missing/partial (defensive — old rows, manual inserts).
    """
    desc = skill.external_resources or {}
    source = desc.get("federation_source")
    slug = desc.get("external_slug")
    if source and slug:
        return str(source), str(slug)
    parts = str(skill.slug).split(":", 2)
    if len(parts) == 3 and parts[0] == EXTERNAL_SLUG_PREFIX:
        return parts[1], parts[2]
    return None
