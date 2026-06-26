"""Recipify endpoint — v7 Phase G.

POST /api/recipify
  body: RecipifyIn (slug, content, target_cookbook_id?, visibility, target_subrecipe_id?)
  returns: RecipifyOut (slug, cookbook_id, category, related_skills, status)

Auth: x-api-key — Pro+ tier required (Free → 401).
Pro+ with target_subrecipe_id forwards to Phase-C subrecipe scope when wired
(currently writes to the cookbook level with a stub note).
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.bundle_routes import CookbookCtx, require_cookbook_tier  # compat-alias
from app.database import get_db
from app.models import Bundle
from app.recipify import (
    ValidationError,
    classify_skill,
    infer_related_skills,
    validate_frontmatter,
    write_cookbook_skill,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["recipify"])


class RecipifyIn(BaseModel):
    slug: str
    content: str
    target_cookbook_id: UUID | None = None
    visibility: Literal["private", "public_pending_review"] = "private"
    target_subrecipe_id: UUID | None = None


class RecipifyOut(BaseModel):
    slug: str
    cookbook_id: UUID
    category: str
    related_skills: list[str]
    status: Literal["created", "updated"]


def _resolve_or_create_cookbook(db: Session, ctx: CookbookCtx, target_cookbook_id: UUID | None) -> Bundle:
    if target_cookbook_id is not None:
        cb = db.query(Bundle).filter(Bundle.id == target_cookbook_id).first()
        if cb is None:
            raise HTTPException(status_code=404, detail="cookbook_not_found")
        if not ctx.is_master and cb.bundle_owner != ctx.user_id:
            raise HTTPException(status_code=404, detail="cookbook_not_found")
        return cb

    cb = (
        db.query(Bundle)
        .filter(Bundle.bundle_owner == ctx.user_id)  # compat-alias
        .order_by(Bundle.created_at.asc())
        .first()
    )
    if cb is None:
        cb = Bundle(
            id=uuid4(),
            name="My Bundle",
            bundle_owner=ctx.user_id,
            is_base=False,
        )
        db.add(cb)
        db.commit()
        db.refresh(cb)
    return cb


@router.post("/recipify", response_model=RecipifyOut)
def recipify(
    body: RecipifyIn,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Validate and store a new SKILL.md draft as a CookbookSkill."""
    # evergreen_0206 Phase G: require_cookbook_tier now ADMITS free (the bundle  # compat-alias
    # on-ramp). Recipify is an AUTHORING capability, not part of the free taste
    # (free = install + one bundle + one sync). Keep authoring paid-gated with an
    # explicit check so opening the shared gate didn't accidentally expose it.
    from app.tier_labels import _is_paid_tier

    if not ctx.is_master and not _is_paid_tier(ctx.tier):
        raise HTTPException(
            status_code=401,
            detail={"needs_tier": "pro", "current_tier": ctx.tier},
        )

    if (
        body.target_subrecipe_id is not None
        and ctx.tier not in ("operator", "pro_plus")
        and not ctx.is_master
    ):  # operator = legacy alias
        raise HTTPException(status_code=403, detail="subrecipe_requires_operator")

    try:
        validate_frontmatter(body.content)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"reason": "invalid_frontmatter", "error": str(exc)})

    cb = _resolve_or_create_cookbook(db, ctx, body.target_cookbook_id)

    classification = classify_skill(body.content)
    related = infer_related_skills(body.content, cb.id, db)

    try:
        cs, status = write_cookbook_skill(
            slug=body.slug,
            content=body.content,
            target_cookbook_id=cb.id,
            visibility=body.visibility,
            db=db,
            classifier=classification,
            related=related,
            owner_user_id=ctx.user_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"reason": "invalid_input", "error": str(exc)})

    if body.target_subrecipe_id is not None:
        # Phase-C wiring is a stub: scope row stays at the bundle level.
        logger.info(
            "recipify: subrecipe scope requested (%s) — Phase C not wired, wrote at cookbook scope instead.",
            body.target_subrecipe_id,
        )

    return RecipifyOut(
        slug=body.slug,
        cookbook_id=cb.id,
        category=classification["category"],
        related_skills=related,
        status=status,
    )
