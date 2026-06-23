"""recipes_search — full-text catalog search with hybrid recall fallback.

Backed by the same ORM query used by ``GET /api/skills/search``. We import the
SQLAlchemy primitives directly rather than calling the FastAPI handler to keep
the MCP path free of HTTP loopback.

Issue #111: when the literal ILIKE pass returns fewer than
``HYBRID_MIN_KEYWORD_HITS`` results AND the caller supplied a non-empty query,
augment with ``recall_skills`` (BM25 + optional vector). This closes the
"recall finds many, search finds zero" gap reported on a broad
multi-keyword dev query.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Skill
from app.routes import _install_counts_for, _skill_to_out

logger = logging.getLogger(__name__)

# Threshold below which we widen the search via hybrid recall.
HYBRID_MIN_KEYWORD_HITS = 3


def recipes_search(
    db: Session,
    query: str | None = None,
    category: str | None = None,
    tier: str | None = None,
    limit: int = 100,
    hybrid: bool = True,
) -> dict[str, Any]:
    """Search the public catalog by keyword, with hybrid fallback when sparse.

    Returns ``{"results": [...], "total": int, "backend": str,
    "hybrid_augmented": bool}``.

    - ``backend = "keyword"`` — literal ILIKE pass alone.
    - ``backend = "hybrid"``  — literal + recall results unioned.
    - ``backend = "recall_only"`` — literal returned zero, recall provided all.

    WIS-948: default limit raised 20->100 so a bare recipes_search() call
    returns the full (or near-full) catalog instead of silently capping at 20.
    The HTTP search endpoint also honours a ?limit= alias for the same reason:
    Pro-tier buyers browsing the catalog saw only 20/63 paid skills they own.
    """
    # Public-scope MCP tool: searches the public skill catalog only; is_public filter applied internally.
    q = (
        db.query(Skill)
        .options(
            joinedload(Skill.versions),
            joinedload(Skill.creator),
        )
        .filter(
            Skill.is_public == True,  # noqa: E712
            Skill.is_archived == False,  # noqa: E712
        )
    )

    if query:
        q = q.filter((Skill.title.ilike(f"%{query}%")) | (Skill.description.ilike(f"%{query}%")))
    if category:
        q = q.filter(Skill.category == category)
    if tier:
        q = q.filter(Skill.tier == tier)

    q = q.order_by(Skill.updated_at.desc())
    total = q.count()
    capped_limit = max(1, min(limit, 100))
    rows = q.limit(capped_limit).all()

    counts = _install_counts_for(db, [s.id for s in rows])
    keyword_results = [_skill_to_out(s, *counts.get(s.id, (0, 0))).model_dump(mode="json") for s in rows]

    backend = "keyword"
    augmented = False
    final_results = keyword_results
    final_total = total

    if hybrid and query and len(rows) < HYBRID_MIN_KEYWORD_HITS:
        try:
            from app.recall_routes import recall_skills

            tier_for_recall = [tier] if tier else ["free", "pro", "pro_plus"]  # canonical slugs (Phase G)
            recall_blob = recall_skills(
                db,
                query=query,
                tier_filter=tier_for_recall,
                limit=max(capped_limit, 10),
                user_id=None,
                is_master=True,
                user_tier=None,
            )
            recall_hits = recall_blob.get("hits", []) if isinstance(recall_blob, dict) else []

            existing_slugs = {s.slug for s in rows}
            extra_slugs = [
                h["slug"]
                for h in recall_hits
                if isinstance(h, dict) and h.get("slug") and h["slug"] not in existing_slugs
            ]

            if extra_slugs:
                extra_q = (
                    db.query(Skill)
                    .options(
                        joinedload(Skill.versions),
                        joinedload(Skill.creator),
                    )
                    .filter(
                        Skill.is_public == True,  # noqa: E712
                        Skill.is_archived == False,  # noqa: E712
                        Skill.slug.in_(extra_slugs),
                    )
                )
                if category:
                    extra_q = extra_q.filter(Skill.category == category)
                extra_rows = {sk.slug: sk for sk in extra_q.all()}
                ordered_extras = [extra_rows[s] for s in extra_slugs if s in extra_rows]

                if ordered_extras:
                    extra_counts = _install_counts_for(db, [s.id for s in ordered_extras])
                    extra_results = [
                        _skill_to_out(s, *extra_counts.get(s.id, (0, 0))).model_dump(mode="json")
                        for s in ordered_extras
                    ]
                    final_results = (keyword_results + extra_results)[:capped_limit]
                    final_total = total + len(extra_results)
                    augmented = True
                    backend = "recall_only" if not rows else "hybrid"
        # Rationale: hybrid recall failure must never kill the keyword-only search path
        except Exception:  # noqa: BLE001
            logger.exception("recipes_search hybrid fallback failed; returning keyword only")

    return {
        "results": final_results,
        "total": final_total,
        "backend": backend,
        "hybrid_augmented": augmented,
    }
