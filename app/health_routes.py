"""Health check route — /api/healthz.

Extracted from app/routes.py (Phase E — secfix_1905).

Registers:
  GET  /healthz  → DB + Stripe webhook lag probe (WIS-1003)
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import StripeEventId
from app.schemas import HealthOut

router = APIRouter(tags=["meta"])

VERSION = "0.5.0"


@router.get("/healthz", tags=["meta"])
def healthz(db: Session = Depends(get_db)):
    """Database liveness + Stripe webhook lag probe."""
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    # Rationale: DB probe; any error → db_status="error" so healthz returns 503
    except Exception:  # noqa: BLE001
        db_status = "error"

    # WIS-1003 (atomic-habits 2026-05-14 #7): expose Stripe webhook lag so
    # the May-12-class incident (signing-secret drift → 17h of silent
    # webhook failures → users charged but plans NULL) becomes a deterministic
    # signal a watchdog can probe in <100ms. Lag = NOW() - max(processed_at).
    # Returns None on cold/empty DB; the watchdog must treat None as "no signal"
    # not "unhealthy" so a freshly-deployed staging env doesn't false-alarm.
    stripe_lag: float | None = None
    stripe_last: str | None = None
    try:
        last_evt: datetime | None = db.execute(
            select(func.max(StripeEventId.processed_at))
        ).scalar_one_or_none()
        if last_evt is not None:
            now = datetime.now(UTC)
            # processed_at is TIMESTAMPTZ in pg; SQLite tests may return naive.
            if last_evt.tzinfo is None:
                last_evt = last_evt.replace(tzinfo=UTC)
            stripe_lag = max(0.0, (now - last_evt).total_seconds())
            stripe_last = last_evt.isoformat()
    # Rationale: Stripe lag probe must never break /healthz; keep it <200ms reliable
    except Exception:  # noqa: BLE001
        # Never let the lag probe break /healthz — it must stay <200ms reliable.
        pass

    return (
        HealthOut(
            status="ok",
            version=VERSION,
            db=db_status,
            stripe_webhook_lag_seconds=stripe_lag,
            stripe_last_event_at=stripe_last,
        )
        if db_status == "ok"
        else JSONResponse(
            status_code=503,
            content=HealthOut(
                status="error",
                version=VERSION,
                db="error",
                stripe_webhook_lag_seconds=stripe_lag,
                stripe_last_event_at=stripe_last,
            ).model_dump(),
        )
    )
