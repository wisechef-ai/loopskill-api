"""Marketing surface counts — single source of truth for catalog stats.

Phase A of top1pct_1105: every public-facing surface (homepage hero, /skills,
/pricing, /docs/getting-started, /docs/mcp) reads from this endpoint instead
of hardcoded numbers. Drift is mechanically impossible.

Phase F extends this with the full marketing snapshot (tier names + endpoints +
tool list) read from config/recipes-marketing.yaml.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Skill
from app.tier_labels import display_label

router = APIRouter(prefix="/api/marketing", tags=["marketing"])


@router.get("/counts")
def marketing_counts(db: Session = Depends(get_db)) -> dict:
    """Live catalog counts — drift-proof source for every public surface.

    Returns:
        total: every non-archived public skill
        free: tier='free'
        pro: tier='cook' (display label "Pro")
        pro_plus: tier='operator' (display label "Pro+")
        pro_plus_exclusive: skills only available on Pro+ (== pro_plus today
            because the Pro tier still gates Pro+ as a strict superset; future
            tier semantics may diverge)
        last_added_at: ISO timestamp of the newest skill
    """
    base = db.query(Skill).filter(
        Skill.is_public == True,  # noqa: E712
        Skill.is_archived == False,  # noqa: E712
    )

    total = base.count()
    by_tier = dict(
        db.query(Skill.tier, func.count(Skill.id))
        .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
        .group_by(Skill.tier)
        .all()
    )

    last_added = (
        db.query(func.max(Skill.created_at))
        .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
        .scalar()
    )

    free = by_tier.get("free", 0)
    cook = by_tier.get("cook", 0)
    operator = by_tier.get("operator", 0) + by_tier.get("studio", 0)
    pro_plus_exclusive = operator  # see docstring; tracked separately for future

    return {
        "total": total,
        "free": free,
        "pro": cook,
        "pro_plus": operator,
        "pro_plus_exclusive": pro_plus_exclusive,
        "last_added_at": last_added.isoformat() if last_added else None,
        # Display labels (single point where DB slugs become brand labels)
        "labels": {
            "free": display_label("free"),
            "pro": display_label("cook"),
            "pro_plus": display_label("operator"),
        },
    }
