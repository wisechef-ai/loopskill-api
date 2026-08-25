"""Unified anonymous search across skills, loops, bundles, personalities, and
connectors.

feat/unified-search: GET /api/search?q=<text>&limit=<per-group> — one call that
searches all five catalog types and returns them grouped by type ("Spotify-style"
search: type your query once, get back sections). Anonymous — registered in the
APIKeyMiddleware public-prefix allow-list (see app/middleware/_public_paths.py)
the same way the existing per-type public routes are.

Query semantics + per-type public-visibility filters are implemented in
app/services/unified_search.py, which copies each filter expression verbatim
from the existing public route for that type so the two surfaces can never
disagree about what's publicly visible. See that module's docstring for the
filter provenance.

NOTE on personalities: Personality DOES have a public-visibility model
(is_public / is_archived, same shape as Skill/Verifier) and a public browse
route (GET /api/personalities), so this group is populated — it is NOT one of
the "no public model, return []" cases.

NOTE on connectors (mesh0408 T1-D): Connector has the same is_public/
is_archived shape and a public browse route (GET /api/connectors). The
underlying table can legitimately be EMPTY (T1-C, a sister phase, populates
rows separately) — an empty connectors list is a correct response, not a
defect. verifiers-as-a-distinct-type and composite_loops have NO group here;
that is a recorded gap (hub.md §3 open question), not an omission to silently
patch over.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.unified_search import (
    search_bundles_group,
    search_connectors_group,
    search_federated_group,
    search_loops_group,
    search_personalities_group,
    search_skills_group,
)

router = APIRouter(tags=["search"])

_MAX_LIMIT = 20
_DEFAULT_LIMIT = 5
_MIN_QUERY_LEN = 2


@router.get("/search")
def unified_search(
    q: str = Query(
        ...,
        min_length=_MIN_QUERY_LEN,
        description=(
            f"Search text, min {_MIN_QUERY_LEN} chars. Shorter values 422 "
            "(FastAPI's Query min_length) rather than returning an empty result — "
            "callers should treat a too-short query as a client error, not a "
            "legitimate zero-result search."
        ),
    ),
    limit: int = Query(
        _DEFAULT_LIMIT,
        ge=1,
        le=_MAX_LIMIT,
        description=f"Max results PER GROUP (not total). Default {_DEFAULT_LIMIT}, max {_MAX_LIMIT}.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Search skills, loops, bundles, personalities, and connectors in one
    anonymous call.

    Response shape (all five keys always present, always lists — never null,
    never invented data for an empty group):

        {
            "query": "tdd",
            "skills": [{"slug", "title", "description", "category", "tier"}, ...],
            "loops": [{"slug", "title", "description", "max_turns", "tool_count", "run_count"}, ...],
            "bundles": [{"slug", "name", "description", "skill_count"}, ...],
            "personalities": [{"slug", "title", "description", "category", "tier"}, ...],
            "connectors": [{"slug", "title", "description", "connector_type"}, ...],
        }

    Worst case is 5 small SELECT queries (skills, loops, bundles, personalities,
    connectors) plus one small grouped aggregate for bundle skill_count — see
    app/services/unified_search.py for the per-type query + ordering detail.
    """
    federated_rows, federated_status = search_federated_group(db, q, limit)
    return {
        "query": q,
        "skills": search_skills_group(db, q, limit),
        "loops": search_loops_group(db, q, limit),
        "bundles": search_bundles_group(db, q, limit),
        "personalities": search_personalities_group(db, q, limit),
        "connectors": search_connectors_group(db, q, limit),
        # Issue #277 Fix B: sixth group, cache-only (never a live federation
        # fan-out from this per-keystroke endpoint — see the service docstring).
        # federated_cache_status distinguishes "no matches" (warm, []) from
        # "index unavailable" (cold) so the portal never renders a silently
        # empty section.
        "federated": federated_rows,
        "federated_cache_status": federated_status,
    }
