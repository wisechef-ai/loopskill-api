"""Marketing surface counts — single source of truth for catalog stats.

Phase A of top1pct_1105: every public-facing surface (homepage hero, /skills,
/pricing, /docs/getting-started, /docs/mcp) reads from this endpoint instead
of hardcoded numbers. Drift is mechanically impossible.

Phase F extends this with the full marketing snapshot (tier names + endpoints +
tool list) read from config/recipes-marketing.yaml.

Phase L (topshelf_2605): demo-funnel endpoints extracted from routes.py and
added to this module as wisechef_router. Same URL paths preserved.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Skill, WiseChefDemoRequest
from app.schemas import DemoCTAOut, DemoRequestIn, DemoRequestOut
from app.tier_labels import display_label

router = APIRouter(prefix="/api/marketing", tags=["marketing"])

# ── WiseChef demo-funnel router ──────────────────────────────────────────────
# Paths mirror the original /api/wisechef/* surface from routes.py.
wisechef_router = APIRouter(prefix="/api/wisechef", tags=["wisechef"])


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

    # Cookbook caps — read from the tiers.yaml SSOT (loopclose_3005 Phase A) so
    # bullets interpolate {pro_cookbooks}/{pro_plus_cookbooks} and can never
    # drift from the number cookbook_routes.py enforces.
    from app.tier_labels import cookbook_limit

    snap["counts"]["pro_cookbooks"] = cookbook_limit("pro")
    snap["counts"]["pro_plus_cookbooks"] = cookbook_limit("pro_plus")

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


# ── WiseChef Demo CTA ────────────────────────────────────────────────────────


@wisechef_router.get("/demo-cta", response_model=DemoCTAOut)
def demo_cta():
    """WiseChef cross-sell CTA for the Recipes marketplace.

    Returns dynamic marketing content for the landing page and carousel.
    """
    return DemoCTAOut(
        headline="Stop managing AI agents. Start earning with them.",
        subheadline="WiseChef runs your AI workflows — content, SEO, reporting — so you focus on clients.",
        cta_text="Book a Free Demo",
        cta_url="https://wisechef.ai/signup",
        social_proof=[
            "Trusted by marketing agencies across Europe",
            "200+ hours saved per month on content workflows",
            "Set up in 15 minutes, not 15 days",
        ],
        tier_from="€499/mo",
    )


@wisechef_router.post("/demo-request", response_model=DemoRequestOut, status_code=201)
def submit_demo_request(
    body: DemoRequestIn,
    db: Session = Depends(get_db),
):
    """Submit a demo request from the Recipes marketplace.

    Stores in wisechef_demo_requests table for follow-up.
    """
    # Check for duplicate email
    existing = (
        db.query(WiseChefDemoRequest)
        .filter(
            WiseChefDemoRequest.email == body.email,
        )
        .first()
    )
    if existing:
        return DemoRequestOut(
            id=existing.id,
            email=existing.email,
            company_name=existing.company_name,
            company_size=existing.company_size,
            source=existing.source,
            status=existing.status,
            created_at=existing.created_at,
        )

    req = WiseChefDemoRequest(
        email=body.email,
        company_name=body.company_name,
        company_size=body.company_size,
        source=body.source,
        message=body.message,
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    return DemoRequestOut(
        id=req.id,
        email=req.email,
        company_name=req.company_name,
        company_size=req.company_size,
        source=req.source,
        status=req.status,
        created_at=req.created_at,
    )
