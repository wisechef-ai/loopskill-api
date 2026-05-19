"""recipes_recipify — Phase G MCP tool.

Wraps the ``app.recipify`` service. The MCP tool input mirrors RecipifyIn from
``app.recipify_routes``; the output mirrors RecipifyOut. Errors surface as
``{"error": ..., "code": ...}`` rather than raising so the MCP transport can
serialize them cleanly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Cookbook
from app.recipify import (
    ValidationError,
    classify_skill,
    infer_related_skills,
    validate_frontmatter,
    write_cookbook_skill,
)


def _coerce_uuid(value) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def recipes_recipify(
    db: Session,
    *,
    slug: str | None = None,
    content: str | None = None,
    target_cookbook_id: str | UUID | None = None,
    visibility: str = "private",
    target_subrecipe_id: str | UUID | None = None,
    user_id: str | UUID | None = None,
    ctx: AuthContext | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Convert a SKILL.md draft into a CookbookSkill row."""
    if not slug:
        return {"error": "slug is required", "code": "missing_slug"}
    if not content:
        return {"error": "content is required", "code": "missing_content"}

    try:
        validate_frontmatter(content)
    except ValidationError as exc:
        return {"error": str(exc), "code": "invalid_frontmatter"}

    cb_id = _coerce_uuid(target_cookbook_id)
    owner_id = _coerce_uuid(user_id)

    # Phase B (Issue #7): use ctx for cookbook ownership; default to master
    # for backwards compat (stdio, legacy callers without ctx).
    if ctx is None:
        ctx = AuthContext(scope="master")

    cb: Cookbook | None = None
    if cb_id is not None:
        cb = db.query(Cookbook).filter(Cookbook.id == cb_id).first()
        if cb is None:
            return {"error": f"cookbook_not_found: {cb_id}", "code": "cookbook_not_found"}
        # Phase B (Issue #7): cookbook ownership check
        if not authz.can_write_cookbook(ctx, cb):
            return {"error": "cookbook_forbidden", "code": "cookbook_forbidden"}
    else:
        if owner_id is not None:
            cb = (
                db.query(Cookbook)
                .filter(Cookbook.cookbook_owner == owner_id)
                .order_by(Cookbook.created_at.asc())
                .first()
            )
        if cb is None:
            cb = Cookbook(id=uuid4(), name="MCP Cookbook", cookbook_owner=owner_id, is_base=False)
            db.add(cb)
            db.commit()
            db.refresh(cb)

    classification = classify_skill(content)
    related = infer_related_skills(content, cb.id, db)

    try:
        cs, status = write_cookbook_skill(
            slug=slug,
            content=content,
            target_cookbook_id=cb.id,
            visibility=visibility,
            db=db,
            classifier=classification,
            related=related,
            owner_user_id=owner_id,
        )
    except ValidationError as exc:
        return {"error": str(exc), "code": "invalid_input"}

    return {
        "slug": slug,
        "cookbook_id": str(cb.id),
        "category": classification["category"],
        "related_skills": related,
        "status": status,
    }
