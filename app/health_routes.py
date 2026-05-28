"""Health check route — /api/healthz.

Extracted from app/routes.py (Phase E — secfix_1905).

Registers:
  GET  /healthz  → DB + Stripe webhook lag probe (WIS-1003)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import StripeEventId, User
from app.schemas import HealthOut

# fleet-heal-0524 t_a488bb1d — single threshold above which
# stripe_webhook_lag_seconds gets a disambiguating label. 1h matches the
# May-12 17h drift signal and is safely above Stripes own webhook retry window.
_STRIPE_LAG_DRIFT_THRESHOLD_S = float(os.environ.get("STRIPE_WEBHOOK_LAG_DRIFT_THRESHOLD_SECONDS", "3600"))
# Look-back window for paid-traffic existence check. 30d is the minimum subscription
# period; if no paid sub fired in 30d the silence is real-world silence, not drift.
_PAID_TRAFFIC_WINDOW_S = float(os.environ.get("STRIPE_PAID_TRAFFIC_WINDOW_SECONDS", str(30 * 86400)))

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
    stripe_label: str | None = None
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
            # fleet-heal-0524 t_a488bb1d: only label when lag breaches the
            # drift threshold — silent below it (the healthy case). One
            # decision point: a paid signup AFTER the last processed webhook
            # is unambiguous evidence subscribed-type traffic was due but
            # did not arrive → drift_suspected. Otherwise the silence is
            # real-world quiet → no_qualifying_traffic. We additionally bound
            # by the 30d window so an ancient orphan paid User (deleted Stripe
            # sub etc.) cannot indefinitely raise the alarm.
            if stripe_lag >= _STRIPE_LAG_DRIFT_THRESHOLD_S:
                # User.created_at is TIMESTAMP WITHOUT TIME ZONE (naive in pg).
                # Pass naive UTC values to avoid SQLAlchemy tz-mix warnings;
                # the column stores UTC by convention.
                cutoff = (now - timedelta(seconds=_PAID_TRAFFIC_WINDOW_S)).replace(tzinfo=None)
                last_evt_naive = last_evt.replace(tzinfo=None)
                # 'paid' = tier set AND not the literal 'free' string. In prod
                # subscription_tier values include NULL and 'free' alongside
                # the paid 'pro' / 'pro_plus' (legacy aliases 'cook' / 'operator' sunset 2026-06-10).
                paid_after_last_webhook = db.execute(
                    select(func.count(User.id)).where(
                        User.subscription_tier.is_not(None),
                        User.subscription_tier != "free",
                        User.created_at >= cutoff,
                        User.created_at > last_evt_naive,
                    )
                ).scalar_one()
                stripe_label = "drift_suspected" if paid_after_last_webhook else "no_qualifying_traffic"
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
            stripe_webhook_lag_label=stripe_label,
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
                stripe_webhook_lag_label=stripe_label,
            ).model_dump(),
        )
    )
