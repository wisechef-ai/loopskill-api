"""API route handlers — shell module (Phase E post-split).

After Phase E (secfix_1905), the major route clusters live in dedicated modules:
  - app/health_routes.py    → GET /healthz
  - app/skill_routes.py     → GET /skills/* (search, trending, detail, graph, etc.)
  - app/install_routes.py   → GET /skills/install + GET /skills/_download
  - app/access_routes.py    → GET /skills/access
  - app/recipe_routes.py    → GET /recipes/{slug} + GET /api-library/{slug}
  - app/utm_redirects.py    → /x/, /li/, /ig/, /yt/, /fb/ short-link redirectors
  - app/_skill_helpers.py   → pure helper functions

This file retains:
  - The APIRouter instance (router) imported by main.py
  - POST /telemetry
  - GET /stats   (marketplace transparency stats)
  - GET /wisechef/demo-cta
  - POST /wisechef/demo-request

Backwards-compat re-exports: anything imported as `from app.routes import X`
in other modules or tests continues to work for one release window.
Tracked for removal in secfix_1906.

Final line count (Phase E): ~150 lines. Plan target was ≤80 aspirationally;
≤150 is acceptable given telemetry + stats + wisechef endpoints stay here
(documented choice per plan §3 Phase E footnote).
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Phase E: backwards-compat re-exports ────────────────────────────────────
# Callers doing `from app.routes import <name>` continue to work for one
# release window. Tracked for removal in secfix_1906.
from app._skill_helpers import (  # noqa: F401
    _UTM_COOKIE_MAX_AGE,
    _UTM_COOKIE_NAME,
    _UTM_REF_ALLOWLIST,
    GRAPH_RAIL_CAP,
    RELATED_SKILLS_CAP,
    _build_manifest,
    _count_today_installs,
    _hydrate_skill_outs,
    _install_counts_for,
    _resolve_caller_tier_for_install,
    _resolve_related,
    _set_utm_ref_cookie,
    _skill_to_out,
)
from app.access_routes import TIER_INSTALL_LIMITS, TIER_RANK  # noqa: F401
from app.database import get_db
from app.install_routes import download_tarball  # noqa: F401
from app.models import (
    InstallEvent,
    Skill,
    TelemetryEvent,
    WiseChefDemoRequest,
)
from app.schemas import (
    DemoCTAOut,
    DemoRequestIn,
    DemoRequestOut,
    TelemetryEventOut,
    TelemetryIn,
)
from app.skill_routes import (  # noqa: F401
    get_full_skill_graph,
    get_skill_detail,
    get_skill_external,
    get_skill_graph,
    get_skill_related,
    search_skills,
    trending_skills,
)
from app.utm_redirects import utm_router  # noqa: F401

# ── Router declaration ───────────────────────────────────────────────────────

router = APIRouter(prefix="/api")

VERSION = "0.5.0"

# WIS-903: Retired skill registry (used by telemetry endpoint)
from pathlib import Path as _Path

_RETIREMENT_FILE = _Path(__file__).resolve().parent.parent / "retired-skills.txt"
_RETIRED_SKILLS: dict[str, str] = {}
if _RETIREMENT_FILE.exists():
    for _line in _RETIREMENT_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#"):
            _parts = _line.split(None, 1)
            if len(_parts) == 2:
                _RETIRED_SKILLS[_parts[0]] = _parts[1]


# ── Telemetry ───────────────────────────────────────────────────────────────


@router.post("/telemetry", status_code=201, tags=["telemetry"], response_model=TelemetryEventOut)
def post_telemetry(
    request: Request,
    body: TelemetryIn,
    db: Session = Depends(get_db),
):
    """Record a telemetry event.

    Accepts two modes (both may be combined):
    - **Typed mode**: typed fields (goal_class, duration_seconds, retry_count,
      user_intervention, agent_class_hash) land in dedicated columns.
    - **Legacy mode**: ``payload`` dict is JSON-serialised into the ``payload``
      text column. Existing callers continue to work unchanged.

    Validation (raises 422 on failure):
    - ``event_type`` ∈ {install, first_use, task_completed, task_failed, replaced}
    - ``duration_seconds`` 0..86400
    - ``agent_class_hash`` regex ^[a-f0-9]{8,64}$ if present

    Raises 404 if ``skill_slug`` is provided but not found in the skills table.
    """
    # Resolve skill_slug → skill_id (required if slug provided)
    skill_id = None
    if body.skill_slug:
        # F5: filter to public skills OR the caller's own private skills to avoid
        # enumeration oracle (201 vs 404 leaking private skill existence)
        api_key_user_id = getattr(request.state, "api_key_user_id", None)
        skill_query = db.query(Skill).filter(Skill.slug == body.skill_slug)
        skill = skill_query.first()
        if not skill:
            raise HTTPException(status_code=404, detail="unknown skill_slug")
        # If skill is private, only creator or admin can log telemetry against it
        if not skill.is_public:
            is_admin = api_key_user_id is None
            is_owner = (
                skill.creator is not None
                and api_key_user_id is not None
                and str(skill.creator.user_id) == str(api_key_user_id)
            )
            if not (is_admin or is_owner):
                # Return 404, not 403 — 403 is also an oracle
                raise HTTPException(status_code=404, detail="unknown skill_slug")
        skill_id = skill.id

    event = TelemetryEvent(
        event_type=body.event_type,
        skill_slug=body.skill_slug,
        # F9: preserve empty dict semantics — {} stores as '{}', not NULL
        payload=json.dumps(body.payload) if body.payload is not None else None,
        # Typed columns (NULL when not provided)
        skill_id=skill_id,
        goal_class=body.goal_class,
        duration_seconds=body.duration_seconds,
        retry_count=body.retry_count,
        user_intervention=body.user_intervention,
        agent_class_hash=body.agent_class_hash,
    )
    db.add(event)

    # RCP-13: keep the denormalised Skill.install_count counter in sync with
    # telemetry. The trending endpoint and live API responses compute counts
    # directly from telemetry/install_events, but Skill.install_count is the
    # popularity scoring input used by the carousel selector and by any future
    # ORDER BY popularity queries. Increment atomically (SQL-level expression,
    # not a Python read-modify-write) so concurrent installs cannot lose
    # writes. Same DB transaction as the telemetry insert — either both land
    # or neither does.
    if body.event_type == "install" and skill_id is not None:
        db.query(Skill).filter(Skill.id == skill_id).update(
            {Skill.install_count: Skill.install_count + 1},
            synchronize_session=False,
        )

    db.commit()
    db.refresh(event)
    return TelemetryEventOut(status="recorded", event_id=str(event.id))


# ── Marketplace stats ────────────────────────────────────────────────────────


@router.get("/stats", tags=["meta"])
def marketplace_stats(db: Session = Depends(get_db)):
    """Public marketplace transparency stats — totals, top categories, top skills.

    No auth required. Powers the /stats portal page and the recipes_stats MCP tool.
    Designed to beat LarryBrain's opacity: full counts, fresh data, clear scope.
    """
    from sqlalchemy import func as _f

    total_skills = db.query(_f.count(Skill.id)).filter(Skill.is_public == True).scalar() or 0
    total_installs = db.query(_f.count(InstallEvent.id)).scalar() or 0

    # Tier breakdown
    tier_rows = (
        db.query(Skill.tier, _f.count(Skill.id)).filter(Skill.is_public == True).group_by(Skill.tier).all()
    )
    by_tier = {t or "uncategorized": int(c) for t, c in tier_rows}

    # Category breakdown
    cat_rows = (
        db.query(Skill.category, _f.count(Skill.id))
        .filter(Skill.is_public == True)
        .group_by(Skill.category)
        .order_by(_f.count(Skill.id).desc())
        .limit(20)
        .all()
    )
    by_category = [{"category": c or "uncategorized", "count": int(n)} for c, n in cat_rows]

    # Top installed skills (lifetime)
    top_rows = (
        db.query(InstallEvent.skill_slug, _f.count(InstallEvent.id).label("installs"))
        .group_by(InstallEvent.skill_slug)
        .order_by(_f.count(InstallEvent.id).desc())
        .limit(10)
        .all()
    )
    top_installed = [{"slug": s, "installs": int(c)} for s, c in top_rows]

    # Recent installs (last 7d)
    recent_window = datetime.now(UTC) - timedelta(days=7)
    installs_7d = (
        db.query(_f.count(InstallEvent.id)).filter(InstallEvent.created_at >= recent_window).scalar() or 0
    )

    # Trending pairs (Stage 2, G16): top-weighted derived edges, deduplicated
    # to undirected pairs. Quietly returns [] when the edge table is empty.
    try:
        from app.models import SkillDerivedEdge

        edge_rows = (
            db.query(SkillDerivedEdge.source_slug, SkillDerivedEdge.target_slug, SkillDerivedEdge.weight)
            .order_by(SkillDerivedEdge.weight.desc())
            .limit(200)  # over-fetch, dedupe pulls roughly half
            .all()
        )
        seen_pairs: set[tuple[str, str]] = set()
        trending_pairs: list[dict] = []
        for src, tgt, w in edge_rows:
            key = tuple(sorted([src, tgt]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            trending_pairs.append({"a": key[0], "b": key[1], "weight": float(w)})
            if len(trending_pairs) >= 10:
                break
    # Rationale: optional analytics query; any DB/type error → empty trending list
    except Exception:  # noqa: BLE001
        trending_pairs = []

    return {
        "total_skills": int(total_skills),
        "total_installs_lifetime": int(total_installs),
        "installs_last_7d": int(installs_7d),
        "by_tier": by_tier,
        "by_category": by_category,
        "top_installed": top_installed,
        "trending_pairs": trending_pairs,
        "generated_at": datetime.now(UTC).isoformat(),
    }


# ── WiseChef Demo CTA ────────────────────────────────────────────────────────


@router.get("/wisechef/demo-cta", response_model=DemoCTAOut, tags=["wisechef"])
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


@router.post("/wisechef/demo-request", response_model=DemoRequestOut, status_code=201, tags=["wisechef"])
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
