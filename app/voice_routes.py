"""Phase FB+VOICE — agent voice wiring + fleet owner inbox (activate_0701).

Routes the EXISTING voice models (FeedbackSubmission, RecipifyRequest) into an
aggregated fleet-level inbox.

RETIRED (t_a7ebcc96, 2026-11-16): the SkillErrorReport rail is deleted. Its only
writer was app/services/sync_report.py reading payload["skill_errors"] from
POST /api/sync-report, and no client has ever sent that key
(scripts/loopskill-collect-reports.py:build_payload() has no skill_errors field),
so the table held 0 rows for its entire existence. Both endpoints here were
already tombstoned on 2026-09-22 (docs/ops/tombstones.md) with 0 requests in the
30-day inventory, so no consumer lost anything, ever.

Endpoints:
  GET  /api/fleets/{fleet_id}/voice-inbox    — paginated aggregated stream
  POST /api/fleets/{fleet_id}/voice-inbox/{item_type}/{item_id}/resolve
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.fleet_routes import resolve_fleet_ctx
from app.models import FeedbackSubmission, Fleet, RecipifyRequest
from app.tombstone import tombstoned  # moneypath-C: 0-use public surfaces (routes kept)

router = APIRouter(prefix="/api/fleets", tags=["voice-inbox"])


def _resolve_owned_fleet(db: Session, fleet_id: str, request: Request) -> Fleet:
    """Resolve fleet ownership (reuse fleet_routes ctx pattern)."""
    ctx = resolve_fleet_ctx(request, db)
    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="fleet_not_found")

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        raise HTTPException(status_code=404, detail="fleet_not_found")

    is_owner = ctx.scope == "master" or (
        ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == fleet.owner_user_id
    )
    if not is_owner:
        raise HTTPException(status_code=404, detail="fleet_not_found")
    return fleet


@router.get("/{fleet_id}/voice-inbox")
@tombstoned(since="2026-09-22", ledger="docs/ops/tombstones.md")
def get_voice_inbox(
    fleet_id: str,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    status_filter: str = Query(default="pending,filed", alias="status"),
    after: str | None = Query(default=None),
) -> dict[str, Any]:
    """Aggregated voice inbox: recipify requests + feedback.

    Reads from RecipifyRequest (pending), FeedbackSubmission (pending) —
    UNION'd by created_at, keyset-paginated.

    The skill_error rail was removed 2026-11-16 (t_a7ebcc96): see module docstring.
    ``status`` values accepted for ``item_type`` are ``recipify_request`` and
    ``feedback``; ``skill_error`` now resolves to 422 invalid_item_type rather
    than silently "resolving" a row in a table that has never held a row.
    """
    fleet = _resolve_owned_fleet(db, fleet_id, request)
    statuses = [s.strip() for s in status_filter.split(",") if s.strip()]

    items: list[dict[str, Any]] = []

    # Simplify: fetch all member ids for this fleet, then filter
    from app.models import FleetMember

    member_ids = [
        m.id
        for m in db.query(FleetMember.id)
        .filter(FleetMember.fleet_id == fleet.id, FleetMember.is_active == True)  # noqa: E712
        .all()
    ]

    # RecipifyRequest (by fleet owner)
    for r in (
        db.query(RecipifyRequest)
        .filter(
            RecipifyRequest.api_key_id.in_(
                [m.api_key_id for m in db.query(FleetMember).filter(FleetMember.fleet_id == fleet.id).all()]
            )
            if member_ids
            else RecipifyRequest.id.is_(None)
        )
        .filter(RecipifyRequest.feedback_status.in_(statuses))
        .order_by(RecipifyRequest.created_at.desc())
        .limit(limit)
        .all()
    ):
        items.append(
            {
                "type": "recipify_request",
                "id": str(r.id),
                "target_name": r.target_name,
                "why_useful": (r.why_useful or "")[:200],
                "status": r.feedback_status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )

    # FeedbackSubmission (by fleet members)
    for r in (
        db.query(FeedbackSubmission)
        .filter(
            FeedbackSubmission.api_key_id.in_(
                [m.api_key_id for m in db.query(FleetMember).filter(FleetMember.fleet_id == fleet.id).all()]
            )
            if member_ids
            else FeedbackSubmission.id.is_(None)
        )
        .filter(FeedbackSubmission.feedback_status.in_(statuses))
        .order_by(FeedbackSubmission.created_at.desc())
        .limit(limit)
        .all()
    ):
        items.append(
            {
                "type": "feedback",
                "id": str(r.id),
                "category": r.category,
                "message": (r.message or "")[:200],
                "status": r.feedback_status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )

    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    page = items[:limit]
    return {"items": page, "next_after": None if len(items) <= limit else str(len(page))}


@router.post("/{fleet_id}/voice-inbox/{item_type}/{item_id}/resolve")
@tombstoned(since="2026-09-22", ledger="docs/ops/tombstones.md")
def resolve_voice_item(
    fleet_id: str,
    item_type: str,
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Mark a voice inbox item as resolved."""
    _resolve_owned_fleet(db, fleet_id, request)
    # "skill_error" is intentionally absent (t_a7ebcc96): the table it named is
    # dropped by this PR, so keeping the key would 404 instead of telling the
    # caller the item type no longer exists.
    model_map = {
        "recipify_request": RecipifyRequest,
        "feedback": FeedbackSubmission,
    }
    model = model_map.get(item_type)
    if model is None:
        raise HTTPException(status_code=422, detail="invalid_item_type")
    try:
        item_uuid = UUID(item_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_item_id")

    item = db.query(model).filter(model.id == item_uuid).first()
    if item is None:
        raise HTTPException(status_code=404, detail="item_not_found")
    item.feedback_status = "resolved"
    db.commit()
    return {"status": "resolved", "id": item_id}
