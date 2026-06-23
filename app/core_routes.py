"""Core API endpoints — telemetry and marketplace stats.

Extracted from app/routes.py (Phase L — topshelf_2605) to let routes.py shrink
to ≤80 lines of backward-compat re-exports.

Endpoints:
  - POST /api/telemetry   → record agent/skill telemetry events
  - GET  /api/stats       → public marketplace transparency stats
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import InstallEvent, Skill, TelemetryEvent
from app.schemas import TelemetryEventOut, TelemetryIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

VERSION = "0.5.0"


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

    from app.models import APIKey

    # portal_0610 R5: exclude archived skills from public counts (was counting
    # is_archived=True rows → /api/stats total_skills disagreed with search).
    _public_skill = (Skill.is_public == True, Skill.is_archived == False)  # noqa: E712

    # portal_0610 B3: exclude synthetic (test/CI) installs from public stats —
    # the same §4.2 exclusion already applied to discover/leaderboard, now also
    # on /api/stats (total_installs, installs_7d, top_installed). An install is
    # organic when api_key_id is NULL (anon) OR its APIKey.is_test is false.
    _organic = _f.coalesce(APIKey.is_test, False).is_(False)

    total_skills = db.query(_f.count(Skill.id)).filter(*_public_skill).scalar() or 0
    total_installs = (
        db.query(_f.count(InstallEvent.id))
        .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
        .filter(_organic)
        .scalar()
        or 0
    )

    # Tier breakdown
    tier_rows = db.query(Skill.tier, _f.count(Skill.id)).filter(*_public_skill).group_by(Skill.tier).all()
    by_tier = {t or "uncategorized": int(c) for t, c in tier_rows}

    # Category breakdown
    cat_rows = (
        db.query(Skill.category, _f.count(Skill.id))
        .filter(*_public_skill)
        .group_by(Skill.category)
        .order_by(_f.count(Skill.id).desc())
        .limit(20)
        .all()
    )
    by_category = [{"category": c or "uncategorized", "count": int(n)} for c, n in cat_rows]

    # Top installed skills (lifetime) — organic only (B3).
    top_rows = (
        db.query(InstallEvent.skill_slug, _f.count(InstallEvent.id).label("installs"))
        .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
        .filter(_organic)
        .group_by(InstallEvent.skill_slug)
        .order_by(_f.count(InstallEvent.id).desc())
        .limit(10)
        .all()
    )
    top_installed = [{"slug": s, "installs": int(c)} for s, c in top_rows]

    # Recent installs (last 7d) — organic only (B3).
    recent_window = datetime.now(UTC) - timedelta(days=7)
    installs_7d = (
        db.query(_f.count(InstallEvent.id))
        .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
        .filter(InstallEvent.created_at >= recent_window, _organic)
        .scalar()
        or 0
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
