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


def _str(value: Any) -> str:
    """A stripped string, or "" for anything that is not a string. Upstream JSON
    is untrusted: a list where a title belongs used to crash the whole merge."""
    return value.strip() if isinstance(value, str) else ""


def _owner_from_canonical(url: Any) -> str:
    """``/<owner>/skills/<slug>`` → ``<owner>`` (ClawHub's canonical page path)."""
    parts = [p for p in _str(url).split("/") if p]
    return parts[0] if len(parts) == 3 and parts[1] == "skills" else ""


def normalize_hit(hit: Any) -> dict[str, Any] | None:
    """Map one ``/api/v1/search`` hit to the legacy browse-row shape, or None.

    None means "do not show": a non-dict, a non-ClawHub mirror row, a row ClawHub
    itself flags as suspicious, a row without a string slug, or a row without an
    owner. The owner is required because a ClawHub deep link without one is a
    soft-404, and because a missing owner made the adapter do a live per-row
    owner lookup — the exact cost that once made a cold fan-out take >90s.
    The suspicious filter is defensive: ClawHub hides those from search today,
    but a deep link to supply-chain-flagged content must never render.
    """
    if not isinstance(hit, dict):
        return None
    install = hit.get("install")
    kind = install.get("kind") if isinstance(install, dict) else None
    if kind is not None and kind != _NATIVE_KIND:
        return None
    skill = _native_skill(hit)
    if skill.get("isSuspicious") is True or hit.get("isSuspicious") is True:
        return None
    slug = _str(hit.get("slug")) or _str(skill.get("slug"))
    if not slug:
        return None
    raw_native = hit.get("native")
    native: dict[str, Any] = raw_native if isinstance(raw_native, dict) else {}
    owner = (
        _str(hit.get("ownerHandle"))
        or _str(native.get("ownerHandle"))
        or _owner_from_canonical(hit.get("canonicalUrl"))
    )
    if not owner:
        return None
    raw_stats = skill.get("stats")
    stats: dict[str, Any] = (
        {k: v for k, v in raw_stats.items() if isinstance(k, str)} if isinstance(raw_stats, dict) else {}
    )
    downloads = hit.get("downloads", stats.get("downloads"))
    if isinstance(downloads, (int, float)) and not isinstance(downloads, bool):
        stats["downloads"] = downloads
    else:
        stats.pop("downloads", None)
    raw_tags = skill.get("tags")
    return {
        "slug": slug,
        "displayName": _str(hit.get("displayName")) or _str(skill.get("displayName")) or slug,
        "summary": _str(hit.get("summary")) or _str(skill.get("summary")),
        "ownerHandle": owner,
        "stats": stats,
        "tags": raw_tags if isinstance(raw_tags, dict) else {},
    }


def _browse_row_ok(row: Any) -> bool:
    return isinstance(row, dict) and bool(_str(row.get("slug"))) and row.get("isSuspicious") is not True


def parse_response(data: Any) -> list[dict[str, Any]]:
    """Rows from either response shape: ``{"results": [...]}`` (search) is
    normalised hit by hit; ``{"items": [...]}`` (browse) passes through, minus
    rows without a string slug and rows flagged suspicious."""
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("results"), list):
        rows = (normalize_hit(h) for h in data["results"])
        return [r for r in rows if r is not None]
    items = data.get("items")
    return [r for r in items if _browse_row_ok(r)] if isinstance(items, list) else []


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
