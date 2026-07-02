"""activate_0701 Phase T — POST /api/sync-report route.

Batched fleet telemetry ingestion: one POST per 30-min cycle carries
loop runs, cron health, and skill errors. Member-key auth only (the
x-api-key MUST resolve to a FleetMember).

Route module is kept thin — all logic is in app/services/sync_report.py
(respects the 600-line pyfile-size gate).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.reconcile_abuse_ceiling import check_reconcile_abuse_ceiling
from app.services.fleet_members import resolve_member_for_key
from app.services.sync_report import MAX_BODY_BYTES, ingest_sync_report

router = APIRouter(prefix="/api", tags=["sync-report"])


class SyncReportIn(BaseModel):
    """Batched sync-report payload — all sections optional.

    cycle_ts: ISO timestamp of the 30-min cycle this report covers.
    lockfile_state: NOT stored as rows (D9); only bumps member liveness.
    loop_runs: raw loop outcome records (capped at 200 server-side).
    skill_errors: agent-reported skill errors (capped at 100).
    cron_health: failures + counts snapshot (failed list capped at 50).
    """

    cycle_ts: str | None = None
    lockfile_state: list[dict[str, Any]] | None = None
    loop_runs: list[dict[str, Any]] | None = None
    skill_errors: list[dict[str, Any]] | None = None
    cron_health: dict[str, Any] | None = None


@router.post("/sync-report")
async def post_sync_report(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Ingest one batched sync-report from a fleet member.

    Auth: x-api-key MUST resolve to an active FleetMember.
    Non-member key -> 403. Anonymous -> 401.
    Rate: reuse check_reconcile_abuse_ceiling per api_key_id.
    Body cap: 256 KB -> 413.
    """
    api_key_id = getattr(request.state, "api_key_id", None)
    auth_ctx = getattr(request.state, "auth_ctx", None)

    # Anonymous -> 401
    if auth_ctx is None and api_key_id is None:
        raise HTTPException(status_code=401, detail="authentication_required")

    # Member resolution — non-member key -> 403
    member = resolve_member_for_key(db, api_key_id)
    if member is None:
        raise HTTPException(status_code=403, detail="member_key_required")

    # Rate limit — reuse the reconcile abuse ceiling (generous, per-key).
    ceiling = check_reconcile_abuse_ceiling(str(api_key_id))
    if not ceiling.allowed:
        raise HTTPException(
            status_code=429,
            detail="rate_limited",
            headers={"Retry-After": str(ceiling.retry_after)},
        )

    # Body size cap (D9: 256 KB).
    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload_too_large")

    # Parse JSON body.
    import json

    # Rationale: malformed JSON from a fleet agent is a client error, not a
    # 500 — return 422 so the emitter retries next cycle with valid data.
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=422, detail="invalid_json")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="invalid_payload")

    recorded, truncated = ingest_sync_report(db, member, payload)

    return {"recorded": recorded, "truncated": truncated}
