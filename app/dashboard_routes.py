"""Phase C (loopskill_activate_0701) — fleet dashboard API endpoint.

Serves the aggregated data the web pane needs: per-member drift status,
failed crons count, voice inbox count, cost-per-accepted-change from rollups.
The portal renders this; the API serves JSON.

GET /api/fleets/{fleet_id}/dashboard
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import authz
from app.database import get_db
from app.fleet_routes import resolve_fleet_ctx
from app.models import (
    CronHealthSnapshot,
    FeedbackSubmission,
    Fleet,
    FleetMember,
    LoopRunDailyRollup,
    ReconcileEvent,
    RecipifyRequest,
    SkillErrorReport,
)

router = APIRouter(prefix="/api/fleets", tags=["dashboard"])


@router.get("/{fleet_id}/dashboard")
def get_fleet_dashboard(
    fleet_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Fleet dashboard: drift + voice + cron + cost per member.

    Reads rollups (not raw) per D9. One query per section, no N+1.
    """
    ctx = resolve_fleet_ctx(request, db)
    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="fleet_not_found")

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        raise HTTPException(status_code=404, detail="fleet_not_found")
    if not authz.can_use_fleet(ctx, fleet):
        raise HTTPException(status_code=404, detail="fleet_not_found")

    members = (
        db.query(FleetMember)
        .filter(FleetMember.fleet_id == fleet.id, FleetMember.is_active == True)  # noqa: E712
        .all()
    )
    member_ids = [m.id for m in members]
    api_key_ids = [m.api_key_id for m in members]

    # Per-member last reconcile event (one grouped query)
    member_status: dict[str, dict[str, Any]] = {}
    if member_ids:
        event_rows = (
            db.query(
                ReconcileEvent.member_id,
                ReconcileEvent.outcome,
                func.max(ReconcileEvent.created_at).label("last_at"),
            )
            .filter(ReconcileEvent.member_id.in_(member_ids))
            .group_by(ReconcileEvent.member_id, ReconcileEvent.outcome)
            .all()
        )
        for m in members:
            events_for_member = [e for e in event_rows if e.member_id == m.id]
            last = max((e for e in events_for_member), key=lambda e: e.last_at, default=None)
            member_status[str(m.id)] = {
                "host": m.host,
                "profile": m.profile,
                "last_outcome": last.outcome if last else None,
                "last_event_at": last.last_at.isoformat() if last and last.last_at else None,
                "is_active": m.is_active,
            }

    # Failed crons count per member (latest snapshot)
    cron_failed: dict[str, int] = {}
    if member_ids:
        cron_rows = (
            db.query(
                CronHealthSnapshot.member_id,
                CronHealthSnapshot.error_count,
            )
            .filter(CronHealthSnapshot.member_id.in_(member_ids))
            .order_by(CronHealthSnapshot.created_at.desc())
            .limit(len(member_ids))
            .all()
        )
        for cr in cron_rows:
            mid = str(cr.member_id)
            if mid not in cron_failed:
                cron_failed[mid] = cr.error_count or 0

    # Voice counts (pending items)
    voice_counts = {"skill_errors": 0, "recipify_requests": 0, "feedback": 0}
    if member_ids:
        voice_counts["skill_errors"] = (
            db.query(func.count(SkillErrorReport.id))
            .filter(SkillErrorReport.member_id.in_(member_ids))
            .filter(SkillErrorReport.feedback_status == "pending")
            .scalar()
            or 0
        )
    if api_key_ids:
        voice_counts["recipify_requests"] = (
            db.query(func.count(RecipifyRequest.id))
            .filter(RecipifyRequest.api_key_id.in_(api_key_ids))
            .filter(RecipifyRequest.feedback_status == "pending")
            .scalar()
            or 0
        )
        voice_counts["feedback"] = (
            db.query(func.count(FeedbackSubmission.id))
            .filter(FeedbackSubmission.api_key_id.in_(api_key_ids))
            .filter(FeedbackSubmission.feedback_status == "pending")
            .scalar()
            or 0
        )

    # Cost-per-accepted-change from rollups
    rollup_row = (
        db.query(
            func.sum(LoopRunDailyRollup.cost_usd_total).label("total_cost"),
            func.sum(LoopRunDailyRollup.accepted_changes).label("total_accepted"),
            func.sum(LoopRunDailyRollup.runs).label("total_runs"),
        )
        .filter(LoopRunDailyRollup.fleet_id == fleet.id)
        .first()
    )
    total_cost = float(rollup_row.total_cost or 0)
    total_accepted = int(rollup_row.total_accepted or 0)
    cost_per_accepted = total_cost / total_accepted if total_accepted > 0 else None

    # spotify_1507 Ph F — Ralph-loop detector: members stuck re-running the same
    # loop with zero accepted_change. Surfaced on the pane so a fleet owner sees a
    # spinning agent without reading raw LoopRun rows.
    from app.services.ralph_detector import find_ralph_loops

    ralph_loops = find_ralph_loops(db, fleet.id)

    return {
        "fleet_id": str(fleet.id),
        "fleet_name": fleet.name,
        "members": [
            {
                **member_status.get(str(m.id), {"host": m.host, "profile": m.profile}),
                "member_id": str(m.id),
                "failed_crons": cron_failed.get(str(m.id), 0),
            }
            for m in members
        ],
        "voice": voice_counts,
        "cost_per_accepted_change": cost_per_accepted,
        "total_cost_usd": total_cost,
        "total_accepted_changes": total_accepted,
        "total_runs": int(rollup_row.total_runs or 0),
        "ralph_loops": ralph_loops,
        "ralph_count": len(ralph_loops),
    }
