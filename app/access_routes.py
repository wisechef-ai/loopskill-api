"""Skills access check route — /api/skills/access.

Extracted from app/routes.py (Phase E — secfix_1905).

Registers:
  GET  /skills/access   — tier-based access check for a skill slug

Also exports:
  TIER_RANK           — canonical tier → numeric rank mapping
  TIER_INSTALL_LIMITS — per-tier daily install limits (WIS-902)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app._skill_helpers import _resolve_caller_tier
from app.database import get_db
from app.models import Skill
from app.schemas import SkillAccessOut

router = APIRouter(tags=["skills"])

# Tier rank lookup — higher rank = more capability. None / unknown = anonymous.
# Phase 5 (RCP-INCIDENT-2026-05-11): canonical slugs are 'pro' and 'pro_plus'.
# Legacy slugs kept for backwards compat until 2026-06-10.
TIER_RANK = {
    None: 0, "free": 0,
    "pro": 1, "cook": 1,           # cook=legacy alias for pro
    "pro_plus": 2, "operator": 2, "studio": 3,  # operator/studio=legacy for pro_plus
}

# WIS-902: Tier-aware install rate limits (installs per day per API key).
# Free/anon: 5, Pro: 100, Pro+: unlimited.
TIER_INSTALL_LIMITS: dict[str | None, int | None] = {
    None: 5,        # anonymous / no API key
    "free": 5,      # free-tier user
    "pro": 100,     # Pro subscriber
    "pro_plus": None,  # unlimited
    # Legacy aliases:
    "cook": 100,    # legacy alias → pro
    "operator": None,  # legacy alias → pro_plus
    "studio": None,    # legacy alias → pro_plus
}


@router.get("/skills/access", response_model=SkillAccessOut, tags=["skills"])
def skill_access(
    request: Request,
    skill: str = Query(..., description="Skill slug to check access for"),
    fork_eligible: bool = Query(
        False,
        description="If true, require Operator+ tier (fork capability) on top of skill-tier access. Forks API ships in a later batch.",
    ),
    db: Session = Depends(get_db),
):
    """Check whether the calling subscriber can access a skill.

    Tier semantics (Plan v5.4 §A.8):
      - Cook subscribers can access any current skill (all skills are
        currently cook-tier or below).
      - Operator subscribers add fork capability — pass ``fork_eligible=true``
        to gate access on it.
      - Studio subscribers add bucket capability (bucket endpoints land in a
        later batch; ``bucket_eligible`` is reported on every response).
    """
    s = db.query(Skill).filter(Skill.slug == skill).first()
    if not s:
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")

    user_tier = _resolve_caller_tier(db, request)
    user_rank = TIER_RANK.get(user_tier, 0)
    # Skills with no explicit tier default to pro — the marketplace baseline.
    skill_rank = TIER_RANK.get(s.tier, TIER_RANK["pro"])

    has_access = s.is_public and user_rank >= skill_rank
    if fork_eligible:
        has_access = has_access and user_rank >= TIER_RANK["pro_plus"]

    return SkillAccessOut(
        slug=s.slug,
        title=s.title,
        has_access=has_access,
        tier=s.tier,
        user_tier=user_tier,
        fork_eligible=user_rank >= TIER_RANK["operator"],
        bucket_eligible=user_rank >= TIER_RANK["studio"],
        latest_version=s.versions[0].semver if s.versions else None,
        license=s.license,
    )
