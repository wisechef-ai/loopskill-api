"""Federation install-resolution — per-source origin SKILL.md resolvers.

federation_0604 install-parity (Adam, 2026-06-04 — option A: server-side
resolution, one SSOT every surface/agent reuses).

The Hermes Skills Hub installs EVERY federated source by resolving content from
ORIGIN at install time (`hermes skills install <source>/<id>` → source.fetch()),
never rehosting. This module is our server-side equivalent: one origin resolver
per installable source, returning ``(source_url, content)`` or ``None``.

Posture (matches Hermes):
  - On-demand only — a resolver fires when a user EXPLICITLY installs a specific
    skill, never as a crawl. Cache-fronted, bounded → light on the server.
  - Nothing is persisted / rehosted. Content is streamed from origin.
  - Unknown/absent license → installable + labelled "community · as-is"
    (Hermes community trust level). An EXPLICIT redistribution-forbidding license
    still downgrades to DEEP_LINK via the adapter/router.

Module split (W0.2 pyfile-size discipline, ≤600 lines): the discovery fetchers
(search) live in ``federation_live``; these install resolvers live here. The two
legacy resolvers (hermes-hub, browse-sh) stay in ``federation_live`` and are
re-exported into the registry below so there is ONE ``get_origin_fetcher``.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

import httpx

from app.services.federation_live import (
    _HTTP_TIMEOUT_S,
    _CATALOG_TTL_S,
    CLAWHUB_SKILLS_URL,
    _cache,
    _safe_json_get,
    browse_sh_origin_skill_md,
    hermes_origin_skill_md,
)

logger = logging.getLogger(__name__)

LOBEHUB_AGENT_URL = "https://chat-agents.lobehub.com/{agent_id}.json"
CLAWHUB_DOWNLOAD_URL = "https://clawhub.ai/api/v1/download"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
GITHUB_TREES_URL = "https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
GITHUB_REPO_URL = "https://api.github.com/repos/{repo}"

_MAX_BUNDLE_FILE_BYTES = 500_000  # skip large binaries in a ZIP bundle (Hermes parity)


def well_known_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """well-known FETCH_ORIGIN resolver. The adapter slug is "host--skillname";
    the SKILL.md lives at https://<host>/.well-known/skills/<name>/SKILL.md.

    Reconstruct host + name from the namespaced slug. host may itself contain
    dashes, so we split on the LAST "--" (adapter joins host + "--" + name).
    """
    if "--" not in (slug or ""):
        return None
    host, _, name = slug.rpartition("--")
    host = host.strip().strip("/")
    name = name.strip()
    if not host or not name:
        return None
    raw_url = f"https://{host}/.well-known/skills/{name}/SKILL.md"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(raw_url)
        if resp.status_code == 200 and resp.text.strip():
            return raw_url, resp.text
    # Rationale: an origin outage must surface as unavailable, never a 500.
    except Exception:  # noqa: BLE001
        logger.warning("well-known origin fetch failed for %s", slug, exc_info=True)
    return None


def _lobehub_convert_to_skill_md(agent: dict[str, Any]) -> str:
    """Port of Hermes LobeHubSource._convert_to_skill_md — byte-faithful.

    LobeHub agents are system-prompt templates; convert to a SKILL.md whose
    Instructions section IS the agent's systemRole.
    """
    meta = agent.get("meta")
    if not isinstance(meta, dict):
        meta = agent
    identifier = agent.get("identifier", "lobehub-agent")
    title = meta.get("title", identifier)
    description = meta.get("description", "")
    tags = meta.get("tags", [])
    config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
    system_role = config.get("systemRole", "")
    tag_list = tags if isinstance(tags, list) else []
    fm_lines = [
        "---",
        f"name: {identifier}",
        f"description: {description[:500]}",
        "metadata:",
        "  recipes:",
        f"    tags: [{', '.join(str(t) for t in tag_list)}]",
        "  lobehub:",
        "    source: lobehub",
        "---",
    ]
    body_lines = [
        f"# {title}",
        "",
        description,
        "",
        "## Instructions",
        "",
        system_role if system_role else "(No system role defined)",
    ]
    return "\n".join(fm_lines) + "\n\n" + "\n".join(body_lines) + "\n"


def lobehub_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """lobehub FETCH_ORIGIN resolver — fetch the agent JSON and convert its
    systemRole into a SKILL.md (Hermes parity). Slug is the agent identifier."""
    agent_id = (slug or "").replace("--", "/").strip("/")
    if not agent_id:
        return None
    url = LOBEHUB_AGENT_URL.format(agent_id=agent_id)
    agent = _safe_json_get(url)
    if not isinstance(agent, dict):
        return None
    content = _lobehub_convert_to_skill_md(agent)
    return url, content


def clawhub_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """clawhub FETCH_ORIGIN resolver — download the version ZIP from
    /api/v1/download?slug=&version=, extract SKILL.md (Hermes parity).

    Bounded: skips files > 500KB and rejects unsafe ZIP member paths.
    """
    real_slug = (slug or "").replace("--", "/").strip("/")
    if not real_slug:
        return None
    # Resolve the latest version from the skill detail.
    detail = _safe_json_get(f"{CLAWHUB_SKILLS_URL}/{real_slug}")
    version = None
    if isinstance(detail, dict):
        lv = detail.get("latestVersion")
        if isinstance(lv, dict):
            version = lv.get("version")
        if not version:
            sk = detail.get("skill") if isinstance(detail.get("skill"), dict) else {}
            tags = sk.get("tags") if isinstance(sk.get("tags"), dict) else {}
            version = tags.get("latest")
    if not version:
        return None
    download_url = f"{CLAWHUB_DOWNLOAD_URL}?slug={real_slug}&version={version}"
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(CLAWHUB_DOWNLOAD_URL, params={"slug": real_slug, "version": version})
        if resp.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size > _MAX_BUNDLE_FILE_BYTES:
                    continue
                member = info.filename
                # Reject path traversal / absolute paths in ZIP members.
                if member.startswith("/") or ".." in member.replace("\\", "/").split("/"):
                    continue
                if member.rsplit("/", 1)[-1] == "SKILL.md":
                    content = zf.read(info).decode("utf-8", errors="replace")
                    if content.strip():
                        return download_url, content
    # Rationale: a download/zip failure must surface as unavailable, never a 500.
    except Exception:  # noqa: BLE001
        logger.warning("clawhub origin fetch failed for %s", slug, exc_info=True)
    return None


def _github_default_branch(repo: str) -> str:
    data = _safe_json_get(GITHUB_REPO_URL.format(repo=repo))
    if isinstance(data, dict) and data.get("default_branch"):
        return str(data["default_branch"])
    return "main"


def skills_sh_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """skills.sh FETCH_ORIGIN resolver — TOKEN-FREE.

    A skills.sh id is "owner/repo/skillId". skills.sh has already told us the
    canonical repo, so we resolve the skill's actual path inside it via the anon
    GitHub trees API (60/hr, cached) — basename-matching the skillId's SKILL.md —
    then fetch the raw content anonymously. No GITHUB_TOKEN needed (only code
    *search* — github-oss — requires a token; raw + trees on a known public repo
    are anon-OK).
    """
    ident = (slug or "").replace("--", "/").strip("/")
    parts = ident.split("/")
    if len(parts) < 3:
        return None
    repo = f"{parts[0]}/{parts[1]}"
    skill_id = parts[-1]
    cache_key = f"skills-sh-path:{repo}:{skill_id}"
    cached = _cache.get(cache_key, _CATALOG_TTL_S)
    branch = _github_default_branch(repo)
    if cached is not None:
        raw_url = cached
    else:
        tree = _safe_json_get(GITHUB_TREES_URL.format(repo=repo, branch=branch))
        if not isinstance(tree, dict):
            return None
        skillmd_paths = [
            t["path"]
            for t in tree.get("tree", [])
            if isinstance(t, dict) and str(t.get("path", "")).endswith("SKILL.md")
        ]
        # Prefer the path whose parent dir basename matches the skillId.
        match = next(
            (p for p in skillmd_paths if p.rsplit("/", 2)[-2:-1] == [skill_id]),
            None,
        ) or next((p for p in skillmd_paths if skill_id in p), None)
        if not match:
            return None
        raw_url = f"{GITHUB_RAW_BASE}/{repo}/{branch}/{match}"
        _cache.put(cache_key, raw_url)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(raw_url)
        if resp.status_code == 200 and resp.text.strip():
            return raw_url, resp.text
    # Rationale: an origin outage must surface as unavailable, never a 500.
    except Exception:  # noqa: BLE001
        logger.warning("skills-sh origin fetch failed for %s", slug, exc_info=True)
    return None


# Map of source_id → (home_module, function_name) for the FETCH_ORIGIN install
# path. Covers EXACTLY the installable sources (Hermes parity). github-oss is
# absent — discovery only until a prod GITHUB_TOKEN lands (code-search gated).
#
# Each fetcher is resolved LAZILY against its HOME module, so monkeypatching the
# function where it's defined (federation_live for the two legacy resolvers,
# this module for the four federation_0604 ones) is honoured by the route. This
# avoids the stale-re-export trap a flat dict-of-refs would create after the
# W0.2 module split.
_ORIGIN_FETCHER_HOMES = {
    "hermes-hub": ("federation_live", "hermes_origin_skill_md"),
    "browse-sh": ("federation_live", "browse_sh_origin_skill_md"),
    "well-known": ("federation_install", "well_known_origin_skill_md"),
    "lobehub": ("federation_install", "lobehub_origin_skill_md"),
    "clawhub": ("federation_install", "clawhub_origin_skill_md"),
    "skills-sh": ("federation_install", "skills_sh_origin_skill_md"),
}


def get_origin_fetcher(source_id: str):
    """Resolve the origin SKILL.md fetcher for a source, lazily against its home
    module. Lazy resolution means monkeypatching the function where it's defined
    is honoured by the route, and there's one source of truth for which sources
    are fetch-origin-installable.
    """
    entry = _ORIGIN_FETCHER_HOMES.get(source_id)
    if entry is None:
        return None
    import importlib

    mod_name, fn_name = entry
    mod = importlib.import_module(f"app.services.{mod_name}")
    return getattr(mod, fn_name, None)


# Backwards-compatible direct mapping (built once). Prefer get_origin_fetcher()
# in the route so test monkeypatching of the underlying function is honoured.
ORIGIN_FETCHERS = {
    "hermes-hub": hermes_origin_skill_md,
    "browse-sh": browse_sh_origin_skill_md,
    "well-known": well_known_origin_skill_md,
    "lobehub": lobehub_origin_skill_md,
    "clawhub": clawhub_origin_skill_md,
    "skills-sh": skills_sh_origin_skill_md,
}
