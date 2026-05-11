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


@router.get("/snapshot")
def marketing_snapshot(db: Session = Depends(get_db)) -> dict:
    """Full marketing SSOT — counts merged with config/recipes-marketing.yaml.

    Phase F of top1pct_1105: every public surface should read from this
    endpoint OR from the yaml at build time. The yaml is the static base;
    counts are live-overlaid. Drift watchdog (recipes-publish-watchdog cron,
    every 4h) verifies the yaml matches DB and surfaces.
    """
    import yaml
    from pathlib import Path

    yaml_path = Path(__file__).resolve().parent.parent / "config" / "recipes-marketing.yaml"
    try:
        with open(yaml_path) as f:
            snap = yaml.safe_load(f) or {}
    except FileNotFoundError:
        snap = {"version": 0, "error": "recipes-marketing.yaml missing"}

    # Overlay live counts on top of the yaml's static fallback.
    live = marketing_counts(db)
    snap.setdefault("counts", {})
    snap["counts"]["skills_total"] = live["total"]
    snap["counts"]["free_skills"] = live["free"]
    snap["counts"]["pro_skills"] = live["pro"]
    snap["counts"]["pro_plus_exclusive_skills"] = live["pro_plus"]
    snap["counts"]["last_added_at"] = live["last_added_at"]
    snap["_source"] = "config/recipes-marketing.yaml + live DB counts"
    return snap
