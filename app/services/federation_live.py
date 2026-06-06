"""Live catalog fetch wiring for federation adapters — evergreen_0206 Phase F2.

Phase F shipped the *pure parser* adapters (app/services/federation_adapters.py)
with the HTTP fetch INJECTED so the mapping logic is unit-testable offline. This
module provides the real, network-backed fetch callables + a small TTL cache, so
the /api/skills/external route returns real results instead of the empty default
(`lambda q: []`).

Two live sources this sprint (Adam q4):
  - hermes-hub : the reference catalog. Hermes Hub is a static Docusaurus site;
                 the machine-readable catalog is the HTML table at
                 /docs/reference/skills-catalog (Skill | Description | Path).
                 The whole NousResearch/hermes-agent repo is MIT-licensed, so
                 every bundled skill is fetch-origin / redistributable.
  - github-oss : GitHub code-search for `filename:SKILL.md` repos. Requires a
                 GITHUB_TOKEN (anon code-search is 401). Degrades GRACEFULLY to
                 an empty list when no token is configured — the route still
                 works (Hermes Hub carries it) and GitHub lights up the moment a
                 token lands in the environment. No personal token is ever baked
                 into the process; prod reads a dedicated fine-grained token.

Honesty rule (Phase F5): this module separates INDEXED (everything discovered)
from INSTALLABLE (the redistributable / registerable subset). It never conflates
them. The Hermes catalog size is cheap to count (cached static table); the GitHub
indexed count is search-result-bound and reported per query, never extrapolated.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from html import unescape
from typing import Any
from urllib.parse import urlencode

import httpx

from app.services.federation_fetch import guarded_get

logger = logging.getLogger(__name__)

# ─────────────────────────────── Config ─────────────────────────────────

HERMES_CATALOG_URL = "https://hermes-agent.nousresearch.com/docs/reference/skills-catalog"
HERMES_SKILL_BASE = "https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled"
# The whole hermes-agent repo is MIT (verified 2026-06-03 via GitHub license API).
HERMES_REPO_LICENSE = "MIT"
# Raw fetch-origin base: the MIT repo's SKILL.md files are fetchable here. A
# bundled skill's repo path is skills/<slug-with-double-dash→slash>/SKILL.md.
HERMES_RAW_BASE = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/skills"

GITHUB_CODE_SEARCH_URL = "https://api.github.com/search/code"

# ── federation_0604 — Hermes Skills Hub parity source endpoints ──────────
# Verified live 2026-06-04 against each source's real catalog API. Schemas are
# documented on the matching adapter in federation_adapters.py.
SKILLS_SH_SEARCH_URL = "https://skills.sh/api/search"
SKILLS_SH_SITEMAP_URL = "https://www.skills.sh/sitemap.xml"  # cheap indexed-count probe
CLAWHUB_SKILLS_URL = "https://clawhub.ai/api/v1/skills"
LOBEHUB_INDEX_URL = "https://chat-agents.lobehub.com/index.json"
BROWSE_SH_CATALOG_URL = "https://browse.sh/api/skills"
BROWSE_SH_DETAIL_URL = "https://browse.sh/api/skills/{slug}"

_HTTP_TIMEOUT_S = 12.0
_HERMES_TTL_S = 3600.0  # static catalog — refresh hourly
_GITHUB_TTL_S = 300.0  # per-query search — short cache to stay polite
_CATALOG_TTL_S = 1800.0  # browse-sh / lobehub static catalogs — refresh every 30 min
_SEARCH_TTL_S = 300.0  # skills.sh / clawhub per-query search

# ───────────────────────────── Tiny TTL cache ───────────────────────────


class _TTLCache:
    """Thread-safe value cache keyed by string, with per-entry TTL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        with self._lock:
            hit = self._store.get(key)
        if hit is None:
            return None
        ts, val = hit
        if (time.monotonic() - ts) > ttl:
            return None
        return val

    def put(self, key: str, val: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), val)

    def clear(self) -> None:  # test hook
        with self._lock:
            self._store.clear()


_cache = _TTLCache()


# ─────────────────────────── Hermes Hub fetch ───────────────────────────

# One <tr> → three <td> cells: Skill | Description | Path
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(cell: str) -> str:
    return unescape(_TAG_RE.sub("", cell)).strip()


def _parse_hermes_catalog(html: str) -> list[dict[str, Any]]:
    """Parse the Hermes Hub skills-catalog HTML table into adapter rows.

    Adapter row shape (consumed by HermesHubAdapter._map):
      {slug, title, description, url, license}
    """
    rows: list[dict[str, Any]] = []
    for raw in _ROW_RE.findall(html):
        cells = [_clean(c) for c in _CELL_RE.findall(raw)]
        if len(cells) < 3:
            continue
        skill, description, path = cells[0], cells[1], cells[2]
        # Skip the header row and any malformed entries.
        if not path or path.lower() == "path" or skill.lower() == "skill":
            continue
        # Collision-safe slug from the bundled path (e.g. "apple/findmy").
        slug = path.replace("/", "--")
        rows.append(
            {
                "slug": slug,
                "title": skill,
                "description": description,
                "url": f"{HERMES_SKILL_BASE}/{path}",
                "license": HERMES_REPO_LICENSE,  # whole repo is MIT
            }
        )
    return rows


def _load_hermes_catalog() -> list[dict[str, Any]]:
    """Load (and cache) the full Hermes Hub catalog. Empty list on failure."""
    cached = _cache.get("hermes:catalog", _HERMES_TTL_S)
    if cached is not None:
        return cached
    resp = guarded_get(HERMES_CATALOG_URL, timeout=_HTTP_TIMEOUT_S)
    if resp is None or resp.status_code >= 400:
        logger.warning("hermes-hub catalog fetch failed; returning empty")
        return []
    catalog = _parse_hermes_catalog(resp.text)
    if catalog:
        _cache.put("hermes:catalog", catalog)
    return catalog


def hermes_hub_fetch(query: str) -> list[dict[str, Any]]:
    """Fetch callable for HermesHubAdapter — substring filter over the catalog.

    Empty query returns the whole catalog (the adapter applies its own limit).
    """
    catalog = _load_hermes_catalog()
    q = (query or "").strip().lower()
    if not q:
        return catalog
    out = [
        r
        for r in catalog
        if q in r.get("title", "").lower()
        or q in r.get("description", "").lower()
        or q in r.get("slug", "").lower()
    ]
    return out


def hermes_indexed_count() -> int:
    """Cheap indexed-count for the teaser (cached static catalog)."""
    return len(_load_hermes_catalog())


def hermes_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """Fetch the real SKILL.md content for a Hermes-Hub skill, from origin.

    This is the fetch-origin install path made real: the whole hermes-agent repo
    is MIT, so the bundled SKILL.md is redistributable. The bundled repo path is
    skills/<slug with "--" → "/">/SKILL.md on the MIT repo's raw host.

    Returns (raw_url, content) on success, or None when the slug doesn't resolve
    to a real SKILL.md (so the caller can 404 honestly rather than fabricate).
    """
    path = (slug or "").replace("--", "/").strip("/")
    if not path:
        return None
    raw_url = f"{HERMES_RAW_BASE}/{path}/SKILL.md"
    resp = guarded_get(raw_url, timeout=_HTTP_TIMEOUT_S)
    if resp is None or resp.status_code != 200 or not resp.text.strip():
        return None
    return raw_url, resp.text


# ─────────────────────────── GitHub OSS fetch ───────────────────────────


def _github_token() -> str | None:
    # Accept either GITHUB_TOKEN or GH_TOKEN; never a hardcoded value.
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def github_oss_fetch(query: str) -> list[dict[str, Any]]:
    """Fetch callable for GitHubOSSAdapter via code-search on filename:SKILL.md.

    Returns adapter rows: {full_name, name, license:{spdx_id}, has_skill_md,
    html_url, description}. has_skill_md is TRUE by construction (the
    `filename:SKILL.md` qualifier guarantees the file exists). License comes from
    the repository object when GitHub includes it; absent → unknown → the adapter
    treats it as non-redistributable (deep-link, conservative).

    Degrades to [] when no token is configured (anon code-search is 401).
    """
    token = _github_token()
    if not token:
        logger.info("github-oss fetch skipped: no GITHUB_TOKEN configured (graceful empty)")
        return []

    q = (query or "").strip()
    cache_key = f"github:{q.lower()}"
    cached = _cache.get(cache_key, _GITHUB_TTL_S)
    if cached is not None:
        return cached

    # `filename:SKILL.md` guarantees a real skill manifest; the user query
    # narrows it. Code search returns code items each carrying a repository obj.
    search_q = f"{q} filename:SKILL.md" if q else "filename:SKILL.md"
    headers = {
        "Accept": "application/vnd.github.text-match+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(
                GITHUB_CODE_SEARCH_URL,
                params={"q": search_q, "per_page": 30},
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
    # Rationale: a GitHub outage / rate-limit must NEVER 500 the route.
    except Exception:  # noqa: BLE001
        logger.warning("github-oss code-search failed; returning empty", exc_info=True)
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload.get("items", []):
        repo = item.get("repository") or {}
        full_name = repo.get("full_name")
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)
        # Repository.license is present in the search payload when GitHub has it.
        lic = repo.get("license") or None
        rows.append(
            {
                "full_name": full_name,
                "name": repo.get("name", full_name),
                "license": lic,  # {"spdx_id": "..."} or None → adapter handles both
                "has_skill_md": True,  # guaranteed by filename:SKILL.md
                "html_url": repo.get("html_url", f"https://github.com/{full_name}"),
                "description": repo.get("description") or "",
            }
        )
    _cache.put(cache_key, rows)
    return rows


# ───────────────── federation_0604 — Hermes Hub parity fetchers ──────────
#
# Each fetcher returns the adapter row shape documented on its adapter in
# federation_adapters.py, and degrades GRACEFULLY to [] on any error (a source
# outage must never 500 the /api/skills/external route — the other sources carry
# it). Static catalogs (browse-sh, lobehub) are cached + substring-filtered
# locally; per-query APIs (skills.sh, clawhub) hit the network with a short TTL.


def _safe_json_get(url: str, *, params: dict | None = None, headers: dict | None = None) -> Any | None:
    """GET + JSON parse with a hard timeout, SSRF-guarded. None on any failure.

    superset_0606 Phase A: every federation JSON fetch flows through the
    SSRF/redirect guard (``guarded_get``) so a poisoned catalog URL or a 302 to
    a private/metadata host is blocked before the request lands. ``params`` are
    folded into the URL because ``guarded_get`` issues the request itself.
    """
    full_url = url
    if params:
        sep = "&" if ("?" in url) else "?"
        full_url = f"{url}{sep}{urlencode(params)}"
    resp = guarded_get(full_url, timeout=_HTTP_TIMEOUT_S, headers=headers)
    if resp is None or resp.status_code >= 400:
        return None
    try:
        return resp.json()
    # Rationale: bad/empty JSON from an origin must never 500 the route.
    except Exception:  # noqa: BLE001
        logger.warning("federation fetch returned non-JSON: %s", url, exc_info=True)
        return None


# ── skills.sh (DEEP_LINK aggregator) ─────────────────────────────────────


def skills_sh_fetch(query: str) -> list[dict[str, Any]]:
    """Fetch callable for SkillsShAdapter. /api/search → {skills:[{id, skillId,
    name, source, installs}]}. Empty query returns []  (the firehose sitemap
    walk is reserved for a future bulk indexer, not the live toggle)."""
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"skills-sh:{q.lower()}"
    cached = _cache.get(cache_key, _SEARCH_TTL_S)
    if cached is not None:
        return cached
    data = _safe_json_get(SKILLS_SH_SEARCH_URL, params={"q": q, "limit": 100})
    rows = data.get("skills", []) if isinstance(data, dict) else []
    rows = rows if isinstance(rows, list) else []
    _cache.put(cache_key, rows)
    return rows


# ── ClawHub (DEEP_LINK community registry) ───────────────────────────────


def clawhub_fetch(query: str) -> list[dict[str, Any]]:
    """Fetch callable for ClawHubAdapter. /api/v1/skills → {items:[{slug,
    displayName, summary, tags, stats}]}."""
    q = (query or "").strip()
    cache_key = f"clawhub:{q.lower()}"
    cached = _cache.get(cache_key, _SEARCH_TTL_S)
    if cached is not None:
        return cached
    params: dict[str, Any] = {"limit": 100}
    if q:
        params["q"] = q
    data = _safe_json_get(CLAWHUB_SKILLS_URL, params=params)
    rows = data.get("items", []) if isinstance(data, dict) else []
    rows = rows if isinstance(rows, list) else []
    _cache.put(cache_key, rows)
    return rows


# ── LobeHub (DEEP_LINK prompt-template marketplace) ──────────────────────


def _load_lobehub_index() -> list[dict[str, Any]]:
    cached = _cache.get("lobehub:index", _CATALOG_TTL_S)
    if cached is not None:
        return cached
    data = _safe_json_get(LOBEHUB_INDEX_URL)
    agents = data.get("agents", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    agents = agents if isinstance(agents, list) else []
    if agents:
        _cache.put("lobehub:index", agents)
    return agents


def lobehub_fetch(query: str) -> list[dict[str, Any]]:
    """Fetch callable for LobeHubAdapter. index.json → {agents:[{identifier,
    homepage, meta:{title, description, tags}}]}. Substring filter on
    title/description/tags."""
    agents = _load_lobehub_index()
    q = (query or "").strip().lower()
    if not q:
        return agents
    out = []
    for a in agents:
        meta = a.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        tags = meta.get("tags", [])
        hay = " ".join(
            [
                str(a.get("identifier", "")),
                str(meta.get("title", "")),
                str(meta.get("description", "")),
                " ".join(tags) if isinstance(tags, list) else "",
            ]
        ).lower()
        if q in hay:
            out.append(a)
    return out


def lobehub_indexed_count() -> int:
    """Cheap indexed-count for the teaser (cached static index)."""
    return len(_load_lobehub_index())


# ── browse.sh (FETCH_ORIGIN site-automation catalog) ─────────────────────


def _load_browse_sh_catalog() -> list[dict[str, Any]]:
    cached = _cache.get("browse-sh:catalog", _CATALOG_TTL_S)
    if cached is not None:
        return cached
    data = _safe_json_get(BROWSE_SH_CATALOG_URL)
    skills = data.get("skills", []) if isinstance(data, dict) else []
    skills = skills if isinstance(skills, list) else []
    if skills:
        _cache.put("browse-sh:catalog", skills)
    return skills


def browse_sh_fetch(query: str) -> list[dict[str, Any]]:
    """Fetch callable for BrowseShAdapter. /api/skills → {skills:[{slug, name,
    title, description, hostname, category, tags}]}. Substring filter."""
    catalog = _load_browse_sh_catalog()
    q = (query or "").strip().lower()
    if not q:
        return catalog
    out = []
    for item in catalog:
        tags = item.get("tags", [])
        hay = " ".join(
            [
                str(item.get("name", "")),
                str(item.get("title", "")),
                str(item.get("description", "")),
                str(item.get("hostname", "")),
                str(item.get("category", "")),
                " ".join(tags) if isinstance(tags, list) else "",
            ]
        ).lower()
        if q in hay:
            out.append(item)
    return out


def browse_sh_indexed_count() -> int:
    """Cheap indexed-count for the teaser (cached static catalog)."""
    return len(_load_browse_sh_catalog())


def browse_sh_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """Fetch the real SKILL.md content for a browse.sh skill, from origin.

    The fetch-origin install path for browse.sh: the per-skill detail endpoint
    (/api/skills/{slug}) returns the SKILL.md inline as ``skillMd`` plus a CDN
    ``skillMdUrl``. We prefer the inline body, falling back to the blob URL.
    The adapter slug is the namespaced form ("host.com--task"); restore the
    original "host.com/task" before hitting the detail endpoint.

    Returns (source_url, content) on success, or None when the slug doesn't
    resolve (so the caller 404s honestly rather than fabricate).
    """
    real_slug = (slug or "").replace("--", "/").strip("/")
    if not real_slug:
        return None
    detail = _safe_json_get(BROWSE_SH_DETAIL_URL.format(slug=real_slug))
    if not isinstance(detail, dict):
        return None
    content = detail.get("skillMd")
    md_url = detail.get("skillMdUrl") or f"{BROWSE_SH_CATALOG_URL}/{real_slug}"
    if isinstance(content, str) and content.strip():
        return md_url, content
    # Fallback: fetch the blob URL directly (SSRF-guarded — md_url is
    # origin-supplied and could point anywhere).
    if isinstance(md_url, str) and md_url.startswith(("http://", "https://")):
        resp = guarded_get(md_url, timeout=_HTTP_TIMEOUT_S)
        if resp is not None and resp.status_code == 200 and resp.text.strip():
            return md_url, resp.text
    return None


# Map of source_id → its live fetch callable (consumed by the route).
LIVE_FETCH = {
    "hermes-hub": hermes_hub_fetch,
    "github-oss": github_oss_fetch,
    "skills-sh": skills_sh_fetch,
    "well-known": lambda _q: [],  # discovery-by-URL only; no central catalog to crawl
    "clawhub": clawhub_fetch,
    "lobehub": lobehub_fetch,
    "browse-sh": browse_sh_fetch,
}

# Map of source_id → cheap indexed-count callable for the off-toggle teaser
# (only sources whose catalog is cheap to count when cached).
INDEXED_COUNT = {
    "hermes-hub": hermes_indexed_count,
    "lobehub": lobehub_indexed_count,
    "browse-sh": browse_sh_indexed_count,
}
