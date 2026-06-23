"""Giants depth crawl — superset_0606 Phase D.

The two federation sources that are 99% of the Hub's headline number, walked to
exhaustion in the background reindex cron so a cold page load never pays the walk
cost (the count is read from the Phase B ``federation_index_cache``):

  - **ClawHub** — a Convex-backed registry. We cursor-walk
    ``GET /api/v1/skills?limit=200&cursor=<nextCursor>`` following ``nextCursor``
    to exhaustion, dedup by slug, with a hard page-cap for safety. Install path is
    **DEEP_LINK only** (decision #6 — ClawHavoc supply-chain incident; we index +
    link, never rehost). ``installable`` is therefore 0 by construction, reported
    honestly and distinctly from ``indexed``.

  - **skills.sh** — walked via its sitemap index. ``sitemap.xml`` points at
    ``sitemap-skills-1.xml`` + ``sitemap-skills-2.xml`` (10k ``<loc>`` each =
    20k slugs). We extract slugs, dedup, and cache the count + first page. The
    per-skill install path stays the shipped ``SkillsShAdapter`` FETCH_ORIGIN
    behaviour (resolves the underlying public GitHub SKILL.md at install time);
    we do NOT resolve all 20k licenses in the bulk walk, so ``installable`` is
    reported as ``None`` ("not resolved in bulk") — never fabricated as 20k.

Every fetch flows through the Phase A SSRF guard (``guarded_get``). The walkers
are pure (injectable ``_get`` for tests) and degrade to a partial/empty result
on any per-page failure rather than aborting the whole reindex run.

Honest-count discipline (decision #5):
  - ``indexed`` = everything discovered (deduped).
  - ``installable`` = the resolved redistributable subset, or ``None`` when the
    bulk walk does not resolve licenses. Never conflated with ``indexed``, never
    invented.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode

from app.services.federation_adapters import ClawHubAdapter, SkillsShAdapter
from app.services.federation_fetch import guarded_get

if TYPE_CHECKING:  # pragma: no cover
    import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────── Config ─────────────────────────────────

CLAWHUB_SKILLS_URL = "https://clawhub.ai/api/v1/skills"
SKILLS_SH_SITEMAP_URL = "https://www.skills.sh/sitemap.xml"

# ClawHub cursor walk bounds. limit=200 is the API max page size; 750 pages caps
# the walk at 150k skills — well above the live count (~50k) so exhaustion wins
# in practice, while the cap guarantees the cron can never run unbounded.
CLAWHUB_PAGE_LIMIT = 200
CLAWHUB_MAX_PAGES = 750

# How many mapped rows to keep in the cache row's first_page (the page UI shows a
# screenful before the user filters; the full walk lives only as a count).
FIRST_PAGE_CAP = 20

_HTTP_TIMEOUT_S = 20.0

# A <loc> in the skills.sh sitemap looks like
#   https://www.skills.sh/<owner>/<repo>/<skill>
# We keep the path after the host as the canonical id (slug = path, "/"→"--").
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_SKILLS_SH_HOST_RE = re.compile(r"^https?://(?:www\.)?skills\.sh/", re.IGNORECASE)


# A getter that takes a fully-built URL and returns an httpx.Response | None
# (matches ``guarded_get``'s contract). Injectable so tests never hit the wire.
Getter = Callable[..., "httpx.Response | None"]


@dataclass
class WalkResult:
    """Outcome of a giants walk — feeds ``federation_cache.write_source_cache``.

    ``installable`` is ``None`` when the walk does not resolve licenses in bulk
    (skills.sh) and ``0`` when the source is DEEP_LINK-only by policy (ClawHub).
    Both are honest, distinct from ``indexed``, and never fabricated.
    """

    indexed: int
    installable: int | None
    first_page: list[dict[str, Any]] = field(default_factory=list)
    pages_walked: int = 0
    exhausted: bool = False
    partial_error: str | None = None


def _build_url(base: str, params: dict[str, Any]) -> str:
    """Fold params into the URL — ``guarded_get`` issues the request itself, so
    the query string (including the URL-encoded ClawHub cursor) is built here."""
    if not params:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def _extract_locs(xml: str) -> list[str]:
    """Pull every ``<loc>`` value out of a sitemap document (index or urlset)."""
    if not xml:
        return []
    return [m.strip() for m in _LOC_RE.findall(xml) if m.strip()]


def _skills_sh_slug(loc: str) -> str | None:
    """Map a skills.sh ``<loc>`` URL to its canonical id (the path after the host).

    ``https://www.skills.sh/vercel-labs/skills/find-skills`` → ``vercel-labs/skills/find-skills``.
    Returns None for non-skill locs (home page, owner pages without a leaf, etc.).
    """
    if not _SKILLS_SH_HOST_RE.match(loc):
        return None
    path = _SKILLS_SH_HOST_RE.sub("", loc).strip("/")
    # A real skill loc has at least owner/.../leaf — at minimum two segments.
    if not path or "/" not in path:
        return None
    return path


# ─────────────────────────────── ClawHub ────────────────────────────────


def clawhub_walk(
    *,
    page_limit: int = CLAWHUB_PAGE_LIMIT,
    max_pages: int = CLAWHUB_MAX_PAGES,
    first_page_cap: int = FIRST_PAGE_CAP,
    _get: Getter | None = None,
) -> WalkResult:
    """Cursor-walk the ClawHub registry to exhaustion (or the page cap).

    Mechanics verified live 2026-06-06: the API returns ``{items, nextCursor}``
    where ``nextCursor`` is an OPAQUE JSON string. The next page is fetched by
    passing it back **verbatim** as ``cursor=<nextCursor>`` (URL-encoded by
    ``urlencode``). Re-serializing it breaks pagination, so we never touch it.

    Termination (any of):
      - the API returns no ``nextCursor`` (true exhaustion → ``exhausted=True``),
      - a page returns zero items,
      - a page yields zero NEW slugs (cursor stalled — defensive guard against an
        API change that would otherwise loop forever),
      - the page cap is hit.

    Install path is DEEP_LINK only (decision #6) → ``installable=0`` honestly.
    """
    get = _get or guarded_get
    adapter = ClawHubAdapter()
    seen: set[str] = set()
    first_page: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    exhausted = False
    partial_error: str | None = None

    while pages < max_pages:
        params: dict[str, Any] = {"limit": page_limit}
        if cursor:
            params["cursor"] = cursor  # verbatim — urlencode handles escaping
        url = _build_url(CLAWHUB_SKILLS_URL, params)
        try:
            resp = get(url, timeout=_HTTP_TIMEOUT_S)
        # Rationale: a single bad page must not abort the whole reindex run; we
        # return what we have so far + record the error honestly.
        except Exception as exc:  # noqa: BLE001
            partial_error = f"clawhub page {pages}: {exc}"[:300]
            logger.warning("clawhub_walk: %s", partial_error)
            break
        if resp is None or resp.status_code != 200:
            partial_error = f"clawhub page {pages}: status={getattr(resp, 'status_code', None)}"
            logger.warning("clawhub_walk: %s", partial_error)
            break
        try:
            data = resp.json()
        # Rationale: a malformed JSON page is a source problem, not ours — stop
        # the walk gracefully and keep the count gathered so far.
        except Exception as exc:  # noqa: BLE001
            partial_error = f"clawhub page {pages}: bad json: {exc}"[:300]
            logger.warning("clawhub_walk: %s", partial_error)
            break

        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list) or not items:
            exhausted = True
            break

        new_this_page = 0
        for row in items:
            if not isinstance(row, dict):
                continue
            slug = row.get("slug") or row.get("displayName")
            if not slug:
                continue
            slug = str(slug)
            if slug in seen:
                continue
            seen.add(slug)
            new_this_page += 1
            if len(first_page) < first_page_cap:
                first_page.append(adapter._map(row).to_dict())

        pages += 1
        cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not cursor:
            exhausted = True
            break
        if new_this_page == 0:
            # Cursor returned but advanced nothing new — defensive stall guard.
            logger.warning("clawhub_walk: cursor stalled at page %d (no new slugs); stopping", pages)
            break

    return WalkResult(
        indexed=len(seen),
        installable=0,  # decision #6: DEEP_LINK only — zero rehost, honest 0
        first_page=first_page,
        pages_walked=pages,
        exhausted=exhausted,
        partial_error=partial_error,
    )


# ─────────────────────────────── skills.sh ──────────────────────────────


def skills_sh_walk(
    *,
    first_page_cap: int = FIRST_PAGE_CAP,
    _get: Getter | None = None,
) -> WalkResult:
    """Walk the skills.sh sitemap index → both ``sitemap-skills-*`` sub-sitemaps.

    The index (``sitemap.xml``) lists sub-sitemaps; we follow every loc whose URL
    contains ``sitemap-skills`` (live: ``-1`` and ``-2``, 10k each = 20k). Slugs
    come from each sub-sitemap's ``<loc>`` paths, deduped.

    ``installable`` is ``None`` — the per-skill FETCH_ORIGIN resolution (shipped
    ``SkillsShAdapter``) happens at install time, not in this bulk count walk, so
    we do not claim a verified installable subset for 20k skills.
    """
    get = _get or guarded_get
    adapter = SkillsShAdapter()
    seen: set[str] = set()
    first_page: list[dict[str, Any]] = []
    partial_error: str | None = None
    sub_count = 0

    try:
        idx_resp = get(SKILLS_SH_SITEMAP_URL, timeout=_HTTP_TIMEOUT_S)
    # Rationale: sitemap-index fetch failure → empty honest result, never a crash.
    except Exception as exc:  # noqa: BLE001
        logger.warning("skills_sh_walk: sitemap index fetch failed: %s", exc)
        return WalkResult(indexed=0, installable=None, partial_error=str(exc)[:300])
    if idx_resp is None or idx_resp.status_code != 200:
        return WalkResult(
            indexed=0,
            installable=None,
            partial_error=f"sitemap index status={getattr(idx_resp, 'status_code', None)}",
        )

    sub_sitemaps = [loc for loc in _extract_locs(idx_resp.text) if "sitemap-skills" in loc.lower()]
    if not sub_sitemaps:
        return WalkResult(indexed=0, installable=None, partial_error="no sitemap-skills sub-sitemaps found")

    for sub_url in sub_sitemaps:
        try:
            sub_resp = get(sub_url, timeout=_HTTP_TIMEOUT_S)
        # Rationale: one sub-sitemap failing must not lose the other's 10k — keep
        # going, record the partial error.
        except Exception as exc:  # noqa: BLE001
            partial_error = f"{sub_url}: {exc}"[:300]
            logger.warning("skills_sh_walk: %s", partial_error)
            continue
        if sub_resp is None or sub_resp.status_code != 200:
            partial_error = f"{sub_url}: status={getattr(sub_resp, 'status_code', None)}"
            logger.warning("skills_sh_walk: %s", partial_error)
            continue
        sub_count += 1
        for loc in _extract_locs(sub_resp.text):
            slug = _skills_sh_slug(loc)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            if len(first_page) < first_page_cap:
                leaf = slug.rsplit("/", 1)[-1]
                owner_repo = slug.rsplit("/", 1)[0]
                row = {"id": slug, "name": leaf, "source": owner_repo}
                first_page.append(adapter._map(row).to_dict())

    return WalkResult(
        indexed=len(seen),
        installable=None,  # bulk walk does not resolve 20k licenses — honest None
        first_page=first_page,
        pages_walked=sub_count,
        exhausted=True,
        partial_error=partial_error,
    )


# Map of source_id → its deep walker. The reindex driver prefers a deep walker
# over the shallow live-search adapter when one is registered here.
DEEP_WALKERS: dict[str, Callable[[], WalkResult]] = {
    "clawhub": clawhub_walk,
    "skills-sh": skills_sh_walk,
}
