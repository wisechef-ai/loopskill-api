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


class _SafeCountDict(dict):
    """dict for str.format_map that leaves unknown ``{token}`` verbatim.

    Lets marketing bullets interpolate live counts (``{pro_skills}`` etc.)
    without ever raising KeyError on copy that contains an unrelated brace.
    """

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "{" + key + "}"


@router.get("/counts")
def marketing_counts(db: Session = Depends(get_db)) -> dict:
    """Live catalog counts — drift-proof source for every public surface.

    Returns:
        total: every non-archived public skill
        free: tier='free'
        pro: tier='pro' (display label "Pro")
        pro_plus: tier='pro_plus' (display label "Pro+")
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
    # Phase G (recipes_2005/G): DB slugs are now 'pro' / 'pro_plus' after migration.
    # Add legacy 'cook'/'operator' counts for any rows not yet migrated (belt-and-suspenders).
    pro = by_tier.get("pro", 0) + by_tier.get("cook", 0)  # cook: 30-day legacy alias
    pro_plus = by_tier.get("pro_plus", 0) + by_tier.get("operator", 0)  # operator: 30-day legacy alias
    pro_plus_exclusive = pro_plus  # see docstring; tracked separately for future

    return {
        "total": total,
        "free": free,
        "pro": pro,
        "pro_plus": pro_plus,
        "pro_plus_exclusive": pro_plus_exclusive,
        "last_added_at": last_added.isoformat() if last_added else None,
        # Display labels (single point where DB slugs become brand labels)
        "labels": {
            "free": display_label("free"),
            "pro": display_label("pro"),
            "pro_plus": display_label("pro_plus"),
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
    from pathlib import Path

    import yaml

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

    # Interpolate {key} placeholders in tier bullets against the live counts so
    # marketing copy numbers (e.g. "{pro_skills} today") track the DB and can
    # never drift stale. Unknown tokens are left verbatim — a stray brace in
    # copy must never raise. See config/recipes-marketing.yaml bullet docs.
    _fmt = _SafeCountDict(snap["counts"])
    for tier in (snap.get("tiers") or {}).values():
        if isinstance(tier, dict) and isinstance(tier.get("bullets"), list):
            tier["bullets"] = [b.format_map(_fmt) if isinstance(b, str) else b for b in tier["bullets"]]

    snap["_source"] = "config/recipes-marketing.yaml + live DB counts"
    return snap
