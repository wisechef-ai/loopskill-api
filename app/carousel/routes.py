"""Carousel API routes.

GET /api/carousel/today       — today's 7 carousel entries
GET /api/carousel/{date}      — carousel for a specific date (YYYY-MM-DD)

Wire format per SPRINT4_CONTRACT.md:
{
  "date": "2026-04-29",
  "entries": [
    {
      "slot": 1,
      "skill": {"slug": ..., "title": ..., "category": ..., "tier": ...,
                "is_free": ..., "vertical": ...},
      "role": "new-capability",
      "tagline": "...",
      "score": 8.4
    },
    ...
  ]
}
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import CarouselEntry

router = APIRouter(tags=["carousel"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _build_carousel_link(slug: str, slot: int, target_date: date) -> str:
    """Return the canonical UTM link for a carousel slot entry.

    Format: /skills/<slug>?ref=carousel-slot-<N>-<YYYYMMDD>
    Used by pick_1605 attribution pipeline and REVENUE_MARKETING posts.
    """
    date_str = target_date.isoformat().replace("-", "")
    return f"/skills/{slug}?ref=carousel-slot-{slot}-{date_str}"




# ── Response schemas ──────────────────────────────────────────────────────


class SkillBrief(BaseModel):
    slug: str
    title: str
    category: str | None = None
    tier: str | None = None
    is_free: bool | None = None
    vertical: str | None = None

    model_config = {"from_attributes": True}


class CarouselEntryItem(BaseModel):
    slot: int | None = None
    skill: SkillBrief
    role: str | None = None
    tagline: str | None = None
    score: float | None = None
    link: str | None = None


class CarouselResponse(BaseModel):
    date: str
    entries: list[CarouselEntryItem]


# ── Helpers ───────────────────────────────────────────────────────────────


def _entries_for_date(target_date: date, db: Session) -> list[CarouselEntry]:
    """Fetch carousel entries for a given date, ordered by slot then position."""
    dt_start = datetime.combine(target_date, datetime.min.time(), tzinfo=UTC)
    dt_end = datetime.combine(target_date, datetime.max.time(), tzinfo=UTC)

    return (
        db.query(CarouselEntry)
        .options(joinedload(CarouselEntry.skill))
        .filter(
            CarouselEntry.featured_date >= dt_start,
            CarouselEntry.featured_date <= dt_end,
        )
        .order_by(
            CarouselEntry.slot,
            CarouselEntry.position,
        )
        .all()
    )


def _build_response(target_date: date, entries: list[CarouselEntry]) -> CarouselResponse:
    items = []
    for e in entries:
        skill = e.skill
        brief = SkillBrief(
            slug=skill.slug,
            title=skill.title,
            category=skill.category,
            tier=skill.tier,
            is_free=getattr(skill, "is_free", None),
            vertical=getattr(skill, "vertical", None),
        )
        slot_num = e.slot if e.slot is not None else e.position + 1
        items.append(
            CarouselEntryItem(
                slot=slot_num,
                skill=brief,
                role=e.role,
                tagline=e.tagline,
                score=e.score,
                link=_build_carousel_link(skill.slug, slot_num, target_date),
            )
        )
    return CarouselResponse(
        date=target_date.isoformat(),
        entries=items,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/carousel/today", response_model=CarouselResponse)
def get_carousel_today(db: Session = Depends(get_db)):
    """Return today's carousel (UTC date)."""
    today = datetime.now(UTC).date()
    entries = _entries_for_date(today, db)
    if not entries:
        raise HTTPException(status_code=404, detail="No carousel entries for today")
    return _build_response(today, entries)


@router.get("/carousel/{date_str}", response_model=CarouselResponse)
def get_carousel_by_date(date_str: str, db: Session = Depends(get_db)):
    """Return carousel for a specific date.

    *date_str* must match ``YYYY-MM-DD`` exactly; anything else yields 422.
    """
    if not _DATE_RE.match(date_str):
        raise HTTPException(
            status_code=422,
            detail="date must match YYYY-MM-DD",
        )
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date is not a valid calendar date",
        )
    entries = _entries_for_date(target_date, db)
    if not entries:
        raise HTTPException(
            status_code=404,
            detail="No entries for that date",
        )
    return _build_response(target_date, entries)
