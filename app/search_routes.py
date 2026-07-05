"""Unified anonymous search across skills, loops, bundles, and personalities.

feat/unified-search: GET /api/search?q=<text>&limit=<per-group> — one call that
searches all four catalog types and returns them grouped by type ("Spotify-style"
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
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.unified_search import (
    search_bundles_group,
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
    """Search skills, loops, bundles, and personalities in one anonymous call.

    Response shape (all four keys always present, always lists — never null,
    never invented data for an empty group):

        {
            "query": "tdd",
            "skills": [{"slug", "title", "description", "category", "tier"}, ...],
            "loops": [{"slug", "title", "description", "max_turns", "tool_count", "run_count"}, ...],
            "bundles": [{"slug", "name", "description", "skill_count"}, ...],
            "personalities": [{"slug", "title", "description", "category", "tier"}, ...],
        }

    Worst case is 4 small SELECT queries (skills, loops, bundles, personalities)
    plus one small grouped aggregate for bundle skill_count — see
    app/services/unified_search.py for the per-type query + ordering detail.
    """
    return {
        "query": q,
        "skills": search_skills_group(db, q, limit),
        "loops": search_loops_group(db, q, limit),
        "bundles": search_bundles_group(db, q, limit),
        "personalities": search_personalities_group(db, q, limit),
    }
