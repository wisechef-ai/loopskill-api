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

import httpx

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

_HTTP_TIMEOUT_S = 12.0
_HERMES_TTL_S = 3600.0  # static catalog — refresh hourly
_GITHUB_TTL_S = 300.0  # per-query search — short cache to stay polite

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
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(HERMES_CATALOG_URL)
            resp.raise_for_status()
        catalog = _parse_hermes_catalog(resp.text)
        if catalog:
            _cache.put("hermes:catalog", catalog)
        return catalog
    # Rationale: a hub outage must NEVER 500 the external-catalog route.
    except Exception:  # noqa: BLE001
        logger.warning("hermes-hub catalog fetch failed; returning empty", exc_info=True)
        return []


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
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(raw_url)
        if resp.status_code != 200 or not resp.text.strip():
            return None
        return raw_url, resp.text
    # Rationale: an origin outage must surface as "unavailable", never a 500.
    except Exception:  # noqa: BLE001
        logger.warning("hermes origin fetch failed for %s", slug, exc_info=True)
        return None


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


# Map of source_id → its live fetch callable (consumed by the route).
LIVE_FETCH = {
    "hermes-hub": hermes_hub_fetch,
    "github-oss": github_oss_fetch,
}
