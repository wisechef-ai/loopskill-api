"""Agent Bootcamp routes — curated, ordered install curricula.

GET /api/bootcamp           — list all tracks (summary cards)
GET /api/bootcamp/{track_id} — one track, steps enriched with LIVE catalog facts

The curriculum prose lives in ``config/bootcamp.yaml`` (the SSOT a builder/agent
edits). At request time each step's ``slug`` is resolved against the live Skill
table so the tier/title/is_free shown always reflect the catalog, never a stale
copy in the yaml. A step whose slug no longer resolves is marked
``available: false`` (and dropped from the public list view) — the bootcamp never
links to a skill you can't install. The yaml's ``tier`` is advisory copy; the
live ``tier`` from the DB wins on the wire (same discipline as marketing_snapshot).

Conversion framing: tracks start on the free on-ramp and cross the free→Pro
boundary at the documented step — the moment "free works" stops working. Each
step carries a UTM-tagged install link (``?ref=bootcamp-<track>-step-<n>``) so
conversion is attributable in the same way carousel slots are.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Skill

router = APIRouter(tags=["bootcamp"])

_BOOTCAMP_YAML = Path(__file__).resolve().parent.parent / "config" / "bootcamp.yaml"


# ── Response schemas ──────────────────────────────────────────────────────


class BootcampStep(BaseModel):
    position: int
    slug: str
    why: str
    # live catalog facts (None when the slug no longer resolves)
    title: str | None = None
    tier: str | None = None
    is_free: bool | None = None
    available: bool = True
    install_link: str | None = None


class BootcampTrackSummary(BaseModel):
    id: str
    title: str
    subtitle: str
    audience: str
    outcome: str
    step_count: int
    free_steps: int
    paid_steps: int


class BootcampTrack(BaseModel):
    id: str
    title: str
    subtitle: str
    audience: str
    outcome: str
    steps: list[BootcampStep]


class BootcampListResponse(BaseModel):
    version: int
    tracks: list[BootcampTrackSummary]


# ── Loader ────────────────────────────────────────────────────────────────


def load_bootcamp_config() -> dict:
    """Read config/bootcamp.yaml. Raises on missing/invalid — fail loud at request."""
    with open(_BOOTCAMP_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "tracks" not in data:
        raise ValueError("bootcamp.yaml missing 'tracks'")
    return data


def _install_link(track_id: str, position: int, slug: str) -> str:
    """UTM-tagged install link so bootcamp conversion is attributable."""
    return f"/skills/{slug}?ref=bootcamp-{track_id}-step-{position}"


def _resolve_steps(track: dict, db: Session) -> list[BootcampStep]:
    """Enrich each yaml step with live catalog facts. Live tier wins over yaml copy."""
    out: list[BootcampStep] = []
    for i, raw in enumerate(track.get("steps", []), start=1):
        slug = raw["slug"]
        skill = (
            db.query(Skill)
            .filter(Skill.slug == slug, Skill.is_archived == False)  # noqa: E712
            .first()
        )
        available = skill is not None
        out.append(
            BootcampStep(
                position=i,
                slug=slug,
                why=raw.get("why", ""),
                title=skill.title if skill else None,
                tier=(skill.tier if skill else None),
                is_free=(getattr(skill, "is_free", None) if skill else None),
                available=available,
                install_link=_install_link(track["id"], i, slug) if available else None,
            )
        )
    return out


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/bootcamp", response_model=BootcampListResponse)
def list_bootcamp_tracks(db: Session = Depends(get_db)):
    """List all bootcamp tracks as summary cards (counts computed from LIVE tiers)."""
    cfg = load_bootcamp_config()
    summaries: list[BootcampTrackSummary] = []
    for track in cfg["tracks"]:
        steps = _resolve_steps(track, db)
        # only count steps that actually resolve to a live skill
        live = [s for s in steps if s.available]
        free_n = sum(1 for s in live if (s.tier or "").lower() == "free")
        summaries.append(
            BootcampTrackSummary(
                id=track["id"],
                title=track["title"],
                subtitle=track["subtitle"],
                audience=track["audience"],
                outcome=track["outcome"],
                step_count=len(live),
                free_steps=free_n,
                paid_steps=len(live) - free_n,
            )
        )
    return BootcampListResponse(version=cfg.get("version", 1), tracks=summaries)


@router.get("/bootcamp/{track_id}", response_model=BootcampTrack)
def get_bootcamp_track(track_id: str, db: Session = Depends(get_db)):
    """Return one track with steps enriched from the live catalog."""
    cfg = load_bootcamp_config()
    track = next((t for t in cfg["tracks"] if t["id"] == track_id), None)
    if track is None:
        raise HTTPException(status_code=404, detail=f"No bootcamp track '{track_id}'")
    return BootcampTrack(
        id=track["id"],
        title=track["title"],
        subtitle=track["subtitle"],
        audience=track["audience"],
        outcome=track["outcome"],
        steps=_resolve_steps(track, db),
    )
