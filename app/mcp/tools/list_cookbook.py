"""recipes_list_cookbook — list a caller's cookbook + skill provenance.

Phase A only ships a read path against the existing ``Bundle`` /
``CookbookSkill`` tables (added in PR #19). The full CRUD endpoints are
Phase B's responsibility.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Bundle, BundleSkill, Skill


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def recipes_list_cookbook(
    db: Session,
    user_id: Any | None = None,
    cookbook_id: str | None = None,
) -> dict[str, Any]:
    """Return the caller's cookbook and skill provenance rows."""
    # Public-scope MCP tool: caller's own bundle; list_cookbook filters by caller's user_id from auth context.  # compat-alias
    cookbook = None
    if cookbook_id:
        cb_uuid = _coerce_uuid(cookbook_id)
        if cb_uuid is not None:
            cookbook = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    elif user_id is not None:
        owner = _coerce_uuid(user_id)
        if owner is not None:
            cookbook = (
                db.query(Bundle)
                .filter(Bundle.bundle_owner == owner)  # compat-alias
                .order_by(Bundle.created_at.desc())
                .first()
            )

    if cookbook is None:
        return {"cookbook": None, "skills": []}

    rows = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cookbook.id)  # compat-alias
        .all()
    )

    return {
        "cookbook": {
            "id": str(cookbook.id),
            "name": cookbook.name,
            "is_base": bool(cookbook.is_base),
            "parent_cookbook_id": (  # compat-alias: legacy field kept for MCP client compat
                str(cookbook.parent_bundle_id) if cookbook.parent_bundle_id else None  # compat-alias
            ),
            "owner": (str(cookbook.bundle_owner) if cookbook.bundle_owner else None),  # compat-alias
        },
        "skills": [
            {
                "skill_id": str(skill.id),
                "slug": skill.slug,
                "title": skill.title,
                "source": cs.source,
                "pinned_version": cs.pinned_version,
            }
            for cs, skill in rows
        ],
    }
