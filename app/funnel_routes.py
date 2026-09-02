"""flywheel_0902/B — HTTP routes for the funnel ledger.

POST /api/funnel/events   — master key or fleet-owner only; idempotent write.
POST /api/funnel/runs     — master key or fleet-owner only; loop-run fact.
GET  /api/funnel/summary  — master key or fleet-owner only (council v2 §0.9c:
                             a public paid:0 feed is a competitor-legible
                             failure signal — NOT public). Per-stage unique
                             stranger/unknown entity counts + event counts,
                             adjacent-stage conversion on unique STRANGER
                             entities only (council v2 §0.9 — the exact
                             false-green fix: two funnel_events rows for the
                             same entity must not double the conversion
                             numerator), paid split into founding_cents
                             (one-time, mode=payment) vs recurring_cents
                             (subscription invoices) — never one total
                             (council v2 §0.9c) — and runs_last_24h per
                             loop_name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Fleet, FunnelEvent, LoopRunLedger
from app.services.funnel_backfill import SOURCE_SYSTEM_STRIPE, SOURCE_SYSTEM_STRIPE_ONETIME
from app.services.funnel_ledger import (
    Classification,
    Stage,
    record_event,
    record_run,
    resolve_entity,
)

router = APIRouter(prefix="/api/funnel", tags=["funnel"])

_STAGES: tuple[Stage, ...] = (
    "lead",
    "contacted",
    "replied",
    "signup",
    "installed",
    "bundle_created",
    "paid",
)

# Adjacent stage pairs, in funnel order — used for the summary's
# conversion-percentage block. Fixed order, not derived, so the summary's
# key set is stable and testable.
_ADJACENT_PAIRS: tuple[tuple[Stage, Stage], ...] = tuple(zip(_STAGES, _STAGES[1:]))


def _resolve_caller_ctx(request: Request, db: Session) -> AuthContext:
    """Resolve the caller's AuthContext exactly as APIKeyMiddleware stamped it.

    Every request always carries a stamped ``request.state.auth_ctx`` (see
    app/middleware/api_key.py) — anonymous when no credential matched. No
    fallback resolution needed here, unlike fleet_routes.resolve_fleet_ctx,
    because funnel write routes accept ONLY master or a logged-in fleet
    owner, never a bare rec_fleet_ member key.
    """
    ctx = getattr(request.state, "auth_ctx", None)
    return ctx if ctx is not None else AuthContext.anonymous()


def _is_fleet_owner(db: Session, ctx: AuthContext) -> bool:
    if ctx.scope != "user" or ctx.user_id is None:
        return False
    owns_a_fleet = db.execute(
        select(Fleet.id).where(Fleet.owner_user_id == ctx.user_id).limit(1)
    ).scalar_one_or_none()
    return owns_a_fleet is not None


def _require_master_or_fleet_owner(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """FastAPI dependency: 401 anonymous, 403 authenticated-but-not-entitled.

    master scope always passes; a user scope caller passes only if they own
    at least one Fleet row. A bare rec_fleet_ member key (scope="fleet") is
    explicitly NOT sufficient — write access to the funnel ledger is a
    fleet-OWNER capability, not a member capability (mirrors
    authz.can_manage_fleet's own owner-vs-member distinction).
    """
    ctx = _resolve_caller_ctx(request, db)
    if ctx.scope == "anonymous":
        raise HTTPException(status_code=401, detail="auth_required")
    if ctx.scope == "master":
        return ctx
    if _is_fleet_owner(db, ctx):
        return ctx
    raise HTTPException(status_code=403, detail="fleet_owner_or_master_required")


# ── POST /api/funnel/events ──────────────────────────────────────────────


class FunnelEventIn(BaseModel):
    stage: str
    source_system: str
    source_event_id: str
    source_loop: str
    host: str
    identifier_kind: str
    identifier_value: str
    classification: str | None = None
    classification_evidence: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    evidence_url: str | None = None
    ts: datetime | None = None


@router.post("/events", status_code=201)
def post_funnel_event(
    body: FunnelEventIn,
    ctx: AuthContext = Depends(_require_master_or_fleet_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if body.stage not in _STAGES:
        raise HTTPException(status_code=422, detail=f"invalid_stage:{body.stage}. Valid: {list(_STAGES)}")
    if body.classification is not None and body.classification not in (
        "fleet",
        "stranger",
        "unknown",
    ):
        raise HTTPException(status_code=422, detail=f"invalid_classification:{body.classification}")

    entity_id = resolve_entity(db, body.identifier_kind, body.identifier_value)  # type: ignore[arg-type]

    classification: Classification = body.classification or "unknown"  # type: ignore[assignment]

    row, replay = record_event(
        db,
        stage=body.stage,  # type: ignore[arg-type]
        entity_id=entity_id,
        source_system=body.source_system,
        source_event_id=body.source_event_id,
        source_loop=body.source_loop,
        host=body.host,
        classification=classification,
        classification_evidence=body.classification_evidence,
        amount_cents=body.amount_cents,
        currency=body.currency,
        evidence_url=body.evidence_url,
        ts=body.ts,
    )
    db.commit()
    return {
        "id": str(row.id),
        "entity_id": str(row.entity_id),
        "stage": row.stage,
        "replay": replay,
    }


# ── POST /api/funnel/runs ────────────────────────────────────────────────


class FunnelRunIn(BaseModel):
    job_id: str
    loop_name: str
    host: str
    outcome: str
    rows_emitted: int = 0
    note: str | None = None
    ts: datetime | None = None


@router.post("/runs", status_code=201)
def post_funnel_run(
    body: FunnelRunIn,
    ctx: AuthContext = Depends(_require_master_or_fleet_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if body.outcome not in ("ok", "no_fire", "error"):
        raise HTTPException(status_code=422, detail=f"invalid_outcome:{body.outcome}")

    row = record_run(
        db,
        job_id=body.job_id,
        loop_name=body.loop_name,
        host=body.host,
        outcome=body.outcome,  # type: ignore[arg-type]
        rows_emitted=body.rows_emitted,
        note=body.note,
        ts=body.ts,
    )
    db.commit()
    return {"id": str(row.id), "job_id": row.job_id, "outcome": row.outcome}


# ── GET /api/funnel/summary ──────────────────────────────────────────────


def _parse_since(since: str | None) -> datetime:
    if not since:
        return datetime.now(UTC) - timedelta(days=7)
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid_since:{since}. Expected ISO-8601.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@router.get("/summary")
def get_funnel_summary(
    since: str | None = Query(default=None),
    ctx: AuthContext = Depends(_require_master_or_fleet_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Master key or fleet-owner only funnel summary. NEVER returns PII.

    Council v2 §0.9c: NOT public — a public ``paid: 0`` feed is a
    competitor-legible failure signal (anyone can watch whether the
    business is making money). Auth-gated identically to the write routes.

    Council v2 §0.9: conversion between adjacent stages is computed on the
    count of UNIQUE STRANGER entities per stage — never on raw event count
    (a re-logged prospect must not double the numerator; the council's
    concrete false-green case: 10 prospects logged twice must yield
    contacted=10, not 20 — see tests/test_funnel_ledger.py).
    """
    since_dt = _parse_since(since)

    stages: dict[str, dict[str, int]] = {}
    unique_stranger_by_stage: dict[str, int] = {}

    for stage in _STAGES:
        total_events = db.execute(
            select(func.count(FunnelEvent.id)).where(FunnelEvent.stage == stage, FunnelEvent.ts >= since_dt)
        ).scalar_one()

        unique_stranger = db.execute(
            select(func.count(func.distinct(FunnelEvent.entity_id))).where(
                FunnelEvent.stage == stage,
                FunnelEvent.ts >= since_dt,
                FunnelEvent.classification == "stranger",
            )
        ).scalar_one()

        unique_unknown = db.execute(
            select(func.count(func.distinct(FunnelEvent.entity_id))).where(
                FunnelEvent.stage == stage,
                FunnelEvent.ts >= since_dt,
                FunnelEvent.classification == "unknown",
            )
        ).scalar_one()

        stages[stage] = {
            "unique_stranger_entities": int(unique_stranger),
            "unique_unknown_entities": int(unique_unknown),
            "events": int(total_events),
        }
        unique_stranger_by_stage[stage] = int(unique_stranger)

    conversions: dict[str, float | None] = {}
    for prev_stage, next_stage in _ADJACENT_PAIRS:
        prev_count = unique_stranger_by_stage[prev_stage]
        next_count = unique_stranger_by_stage[next_stage]
        key = f"{prev_stage}_to_{next_stage}"
        conversions[key] = None if prev_count == 0 else round(100.0 * next_count / prev_count, 1)

    # council v2 §0.9c: split one-time (Founding SKU) from recurring
    # (subscription invoice) paid cents — never a blended total. Discriminated
    # by source_system, which backfill_paid/record_event stamp per Stripe
    # object shape (invoice-backed => recurring, PI-only => one-time). A
    # manually-posted POST /api/funnel/events row must pass the matching
    # source_system to land in the right bucket; anything else (e.g. a
    # caller using bare "stripe") is not counted in either bucket rather
    # than guessed into one.
    founding_cents = int(
        db.execute(
            select(func.coalesce(func.sum(FunnelEvent.amount_cents), 0)).where(
                FunnelEvent.stage == "paid",
                FunnelEvent.ts >= since_dt,
                FunnelEvent.classification == "stranger",
                FunnelEvent.source_system == SOURCE_SYSTEM_STRIPE_ONETIME,
            )
        ).scalar_one()
    )
    recurring_cents = int(
        db.execute(
            select(func.coalesce(func.sum(FunnelEvent.amount_cents), 0)).where(
                FunnelEvent.stage == "paid",
                FunnelEvent.ts >= since_dt,
                FunnelEvent.classification == "stranger",
                FunnelEvent.source_system == SOURCE_SYSTEM_STRIPE,
            )
        ).scalar_one()
    )

    runs_since = datetime.now(UTC) - timedelta(hours=24)
    run_rows = db.execute(
        select(LoopRunLedger.loop_name, func.count())
        .where(LoopRunLedger.ts >= runs_since)
        .group_by(LoopRunLedger.loop_name)
    ).all()
    runs_last_24h = {loop_name: int(count) for loop_name, count in run_rows}

    return {
        "since": since_dt.isoformat(),
        "stages": stages,
        "conversion_pct": conversions,
        "founding_cents": founding_cents,
        "recurring_cents": recurring_cents,
        "runs_last_24h": runs_last_24h,
    }
