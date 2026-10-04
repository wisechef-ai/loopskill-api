"""ClawHub query search — the endpoint that actually honours a query.

ClawHub serves two different read surfaces:

- ``GET /api/v1/skills`` — a cursor-paged BROWSE list. It ignores both ``?q=``
  and ``?search=`` (re-verified live 2026-10-04: ``?search=obsidian`` returns the
  same default page as no parameter at all). The July fix that switched ``q`` to
  ``search`` worked against the API of that day; ClawHub has since moved search
  off this route, so every metasearch since carried up to 25 unrelated rows.
- ``GET /api/v1/search?q=`` — the real ranked search. Each hit already carries
  ``ownerHandle``, ``summary`` and ``downloads``, so mapping a hit needs no
  per-row owner lookup (the old >90s cold-path cost).

The search surface also returns skills.sh MIRRORS (``install.kind ==
"skills-sh"``). Those are dropped here: our own ``skills-sh`` source indexes the
same skills directly, with install counts, so keeping the mirror would show one
skill twice under two names.

``normalize_hit`` maps a search hit into the row shape ``ClawHubAdapter._map``
and ``metasearch.unify_external`` already read (``slug``, ``displayName``,
``summary``, ``ownerHandle``, ``stats.downloads``), so nothing downstream
changes. A response in the legacy ``{"items": [...]}`` shape is accepted as-is,
because that is what the browse route returns and what an upstream rollback
would return.
"""

from __future__ import annotations

from typing import Any, Callable

CLAWHUB_SEARCH_URL = "https://clawhub.ai/api/v1/search"
CLAWHUB_BROWSE_URL = "https://clawhub.ai/api/v1/skills"

# ClawHub answers up to 100 hits per query (verified 2026-10-04). The fan-out
# keeps the top 25 per source, so asking for 100 leaves room for the hits the
# mirror and suspicious filters drop.
SEARCH_LIMIT = 100
BROWSE_LIMIT = 100

_NATIVE_KIND = "clawhub"

JsonGet = Callable[..., Any]


def _native_skill(hit: dict[str, Any]) -> dict[str, Any]:
    native = hit.get("native")
    skill = native.get("skill") if isinstance(native, dict) else None
    return skill if isinstance(skill, dict) else {}


def normalize_hit(hit: Any) -> dict[str, Any] | None:
    """Map one ``/api/v1/search`` hit to the legacy browse-row shape, or None.

    None means "do not show": a non-dict, a non-ClawHub mirror row, a row
    without a slug, or a row ClawHub itself flags as suspicious. The suspicious
    filter is defensive — ClawHub already hides those from search today — but a
    deep link to supply-chain-flagged content is the one row we must never
    render, so the check does not depend on their filter staying on.
    """
    if not isinstance(hit, dict):
        return None
    install = hit.get("install")
    kind = install.get("kind") if isinstance(install, dict) else None
    if kind is not None and kind != _NATIVE_KIND:
        return None
    skill = _native_skill(hit)
    if skill.get("isSuspicious") is True:
        return None
    slug = str(hit.get("slug") or skill.get("slug") or "").strip()
    if not slug:
        return None
    raw_stats = skill.get("stats")
    stats: dict[str, Any] = dict(raw_stats) if isinstance(raw_stats, dict) else {}
    downloads = hit.get("downloads", stats.get("downloads"))
    if downloads is not None:
        stats["downloads"] = downloads
    raw_native = hit.get("native")
    native: dict[str, Any] = raw_native if isinstance(raw_native, dict) else {}
    owner = hit.get("ownerHandle") or native.get("ownerHandle")
    raw_tags = skill.get("tags")
    return {
        "slug": slug,
        "displayName": hit.get("displayName") or skill.get("displayName") or slug,
        "summary": hit.get("summary") or skill.get("summary") or "",
        "ownerHandle": owner if isinstance(owner, str) and owner else None,
        "stats": stats,
        "tags": raw_tags if isinstance(raw_tags, dict) else {},
    }


def parse_response(data: Any) -> list[dict[str, Any]]:
    """Rows from either response shape: ``{"results": [...]}`` (search) is
    normalised hit by hit; ``{"items": [...]}`` (browse) passes through."""
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("results"), list):
        rows = (normalize_hit(h) for h in data["results"])
        return [r for r in rows if r is not None]
    items = data.get("items")
    return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []


def fetch_rows(get_json: JsonGet, query: str) -> list[dict[str, Any]]:
    """Fetch ClawHub rows for ``query`` through the caller's guarded JSON getter.

    A non-empty query goes to the search route. An empty query is a BROWSE (the
    surface that lists ClawHub without a topic), which only the list route
    serves.
    """
    q = (query or "").strip()
    if q:
        data = get_json(CLAWHUB_SEARCH_URL, params={"q": q, "limit": SEARCH_LIMIT})
    else:
        data = get_json(CLAWHUB_BROWSE_URL, params={"limit": BROWSE_LIMIT})
    return parse_response(data)
