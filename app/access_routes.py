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

from app.database import get_db
from app.models import Skill
from app.schemas import SkillAccessOut

router = APIRouter(tags=["skills"])

# Tier rank lookup — higher rank = more capability. None / unknown = anonymous.
# Phase G (recipes_2005/G): canonical slugs simplified to free:1, pro:2, pro_plus:3.
# Legacy aliases 'cook'/'operator'/'studio' kept for READ compat (30-day window, remove after 2026-06-10).
TIER_RANK: dict[str | None, int] = {
    None: 0,  # anonymous / no API key
    "free": 1,
    "pro": 2,  # canonical (Phase 5)
    "pro_plus": 3,  # canonical (Phase 5)
    # 30-day legacy READ aliases (RCP-INCIDENT-2026-05-11, remove after 2026-06-10):
    "cook": 2,  # legacy alias → pro
    "operator": 3,  # legacy alias → pro_plus
    "studio": 3,  # legacy alias → pro_plus (Phase 3 rename, pre-Phase-5)
}

# WIS-902: Tier-aware install rate limits (installs per day per API key).
# None = unlimited. pricing0928 (t_7f5808d2): every tier is unlimited. The old
# table said anon 5 / free 5 / pro 100, but the anon cap never applied
# (``_count_today_installs`` counts per api_key_id, and anonymous callers have
# none), so a signed-in Free key was capped BELOW an anonymous caller. The 6th
# install got a 429 telling the user to upgrade, which contradicts /pricing:
# "every install ... is identical on both". Invariant, pinned by
# tests/test_pricing0928_install_cap_parity.py: no tier's limit is below the
# anonymous one. Anonymous traffic keeps its per-IP minute bucket
# (RateLimitMiddleware).
TIER_INSTALL_LIMITS: dict[str | None, int | None] = {
    None: None,  # anonymous / no API key (never counted: no api_key_id)
    "free": None,  # parity with anonymous
    "pro": None,  # Pro keeps "everything in Free", so it can't sit below Free
    "pro_plus": None,  # unlimited
    # 30-day legacy READ aliases (RCP-INCIDENT-2026-05-11, remove after 2026-06-10):
    "cook": None,  # legacy alias → pro
    "operator": None,  # legacy alias → pro_plus
    "studio": None,  # legacy alias → pro_plus (Phase 3 rename, pre-Phase-5)
}


@router.get("/skills/access", response_model=SkillAccessOut, tags=["skills"])
def skill_access(
    request: Request,
    skill: str = Query(..., description="Skill slug to check access for"),
    fork_eligible: bool = Query(
        False,
        description="If true, require Pro+ tier (fork capability) on top of skill-tier access. Forks API ships in a later batch.",
    ),
    db: Session = Depends(get_db),
):
    """Check whether the calling subscriber can access a skill.

    Tier semantics (Plan v5.4 §A.8, updated Phase G recipes_2005):
      - Pro subscribers can access any current skill.
      - Pro+ subscribers add fork capability — pass ``fork_eligible=true``
        to gate access on it.
      - Legacy slugs 'cook'/'operator' are accepted as READ aliases for 30 days
        (RCP-INCIDENT-2026-05-11, remove after 2026-06-10).
    """
    s = db.query(Skill).filter(Skill.slug == skill).first()
    if not s:
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")

    # Issue HIGH (secfix_1905/I-followup, codex re-pass): access endpoint must not
    # leak title/tier/license for private or archived skills. Treat them as 404 for
    # anonymous and non-master callers. Master scope retains visibility for ops use.
    auth_ctx = getattr(request.state, "auth_ctx", None)
    is_master = getattr(auth_ctx, "scope", None) == "master"
    is_archived = getattr(s, "is_archived", False)
    if (not s.is_public or is_archived) and not is_master:
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")

    user_tier = getattr(auth_ctx, "tier", None)
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
        fork_eligible=user_rank >= TIER_RANK["pro_plus"],
        cookbook_deploy_eligible=user_rank >= TIER_RANK["pro_plus"],
        bucket_eligible=user_rank >= TIER_RANK["pro_plus"],
        latest_version=s.versions[0].semver if s.versions else None,
        license=s.license,
    )
