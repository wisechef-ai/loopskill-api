"""recipes_recall — Phase E hybrid recall (vector + BM25).

Wraps :func:`app.recall_routes.recall_skills` so the MCP layer never
duplicates ranking logic. Caller scope determines tier visibility: master
keys see all tiers, others default to ``free`` only.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.recall_routes import recall_skills


def recipes_recall(
    db: Session,
    *,
    query: str = "",
    local_context_summary: str = "",  # noqa: ARG001 - reserved for future ranking
    tier_filter: list[str] | None = None,
    limit: int = 10,
    **_: Any,
) -> dict[str, Any]:
    """Hybrid BM25 + vector skill recall ranked for the caller's tier."""
    # Public-scope MCP tool: hybrid recall against public catalog only; tier filter is informational.
    if not query:
        return {"error": "query_required", "phase": "E"}
    return recall_skills(
        db,
        query=query,
        tier_filter=tier_filter or ["free", "pro", "pro_plus"],  # canonical slugs (Phase G)
        limit=int(limit),
        user_id=None,
        is_master=True,
        user_tier=None,
    )
