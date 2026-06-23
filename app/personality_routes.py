"""Personality registry routes — deployable persona / SOUL catalog type.

loopskill_0622 Phase 8. A *personality* is a packaged agent identity (system
prompt + config) a user can pull and deploy onto their own agent. Born with clean
LoopSkill vocabulary.

Routes:
  GET  /api/personalities         — browse public personalities
  GET  /api/personalities/{slug}  — personality detail (system prompt + config)
  POST /api/personalities         — publish (auth required)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Personality
from app.schemas import PersonalityDetailOut, PersonalityOut, PersonalityPublishIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/personalities", tags=["personalities"])


def _personality_to_out(p: Personality) -> PersonalityOut:
    latest = p.versions[0].semver if p.versions else None
    creator_name = getattr(p.creator, "display_name", None) if p.creator else None
    creator_handle = getattr(p.creator, "handle", None) if p.creator else None
    return PersonalityOut(
        id=p.id,
        slug=p.slug,
        title=p.title,
        description=p.description,
        category=p.category,
        tier=p.tier,
        is_public=p.is_public,
        creator_name=creator_name,
        creator_handle=creator_handle,
        latest_version=latest,
        install_count=p.install_count or 0,
        rating_avg=p.rating_avg,
        created_at=p.created_at or datetime.now(UTC),
        updated_at=p.updated_at or datetime.now(UTC),
    )


@router.get("", response_model=list[PersonalityOut])
def list_personalities(
    q: str | None = Query(None),
    category: str | None = Query(None),
    limit: int = Query(100, le=200),
    db: Session = Depends(get_db),
) -> list[PersonalityOut]:
    """Browse public, non-archived personalities."""
    query = (
        db.query(Personality)
        .options(joinedload(Personality.versions), joinedload(Personality.creator))
        .filter(Personality.is_public.is_(True), Personality.is_archived.is_(False))
    )
    if category:
        query = query.filter(Personality.category == category)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Personality.title.ilike(like), Personality.description.ilike(like)))
    rows = query.order_by(Personality.install_count.desc()).limit(limit).all()
    return [_personality_to_out(p) for p in rows]


@router.get("/{slug}", response_model=PersonalityDetailOut)
def get_personality(slug: str, db: Session = Depends(get_db)) -> PersonalityDetailOut:
    """Personality detail: system prompt + structured config."""
    p = (
        db.query(Personality)
        .options(joinedload(Personality.versions), joinedload(Personality.creator))
        .filter(Personality.slug == slug)
        .first()
    )
    if p is None or p.is_archived:
        raise HTTPException(status_code=404, detail="personality not found")
    base = _personality_to_out(p).model_dump()
    base.update(
        readme=p.readme,
        license=p.license,
        system_prompt=p.system_prompt,
        config=p.config,
        versions=[
            {
                "id": v.id,
                "semver": v.semver,
                "changelog": v.changelog,
                "tarball_size_bytes": v.tarball_size_bytes,
                "checksum_sha256": v.checksum_sha256,
                "created_at": v.created_at or datetime.now(UTC),
            }
            for v in p.versions
        ],
    )
    return PersonalityDetailOut(**base)


@router.post("", response_model=PersonalityDetailOut, status_code=201)
def publish_personality(
    payload: PersonalityPublishIn,
    request: Request,
    db: Session = Depends(get_db),
) -> PersonalityDetailOut:
    """Publish a personality. Auth required."""
    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None or getattr(ctx, "scope", None) not in ("user", "master"):
        raise HTTPException(status_code=401, detail="authentication required to publish")

    if not (payload.system_prompt or "").strip():
        raise HTTPException(status_code=422, detail="system_prompt is required")

    if db.query(Personality).filter(Personality.slug == payload.slug).first() is not None:
        raise HTTPException(status_code=409, detail=f"personality slug {payload.slug!r} exists")

    p = Personality(
        id=uuid4(),
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        readme=payload.readme,
        license=payload.license,
        tier=payload.tier,
        is_public=payload.is_public,
        system_prompt=payload.system_prompt,
        config=payload.config,
        created_at=datetime.now(UTC),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    logger.info("personality published: %s", p.slug)
    return get_personality(p.slug, db)
