"""recipes_search — full-text catalog search backed by the same ORM query
used by ``GET /api/skills/search``. We import the SQLAlchemy primitives
directly rather than calling the FastAPI handler to keep the MCP path free
of HTTP loopback.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Skill
from app.routes import _install_counts_for, _skill_to_out


def recipes_search(
    db: Session,
    query: str | None = None,
    category: str | None = None,
    tier: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    q = db.query(Skill).options(
        joinedload(Skill.versions),
        joinedload(Skill.creator),
    ).filter(Skill.is_public == True)  # noqa: E712

    if query:
        q = q.filter(
            (Skill.title.ilike(f"%{query}%")) | (Skill.description.ilike(f"%{query}%"))
        )
    if category:
        q = q.filter(Skill.category == category)
    if tier:
        q = q.filter(Skill.tier == tier)

    q = q.order_by(Skill.updated_at.desc())
    total = q.count()
    rows = q.limit(max(1, min(limit, 100))).all()

    counts = _install_counts_for(db, [s.id for s in rows])
    results = [
        _skill_to_out(s, *counts.get(s.id, (0, 0))).model_dump(mode="json")
        for s in rows
    ]
    return {"results": results, "total": total}
