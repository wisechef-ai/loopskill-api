"""Tests for /api/healthz — WIS-1003 stripe_webhook_lag_seconds extension.

The /api/healthz endpoint already existed and returned {status, version, db}.
WIS-1003 (atomic-habits 2026-05-14 #7) extends it with:

  - stripe_webhook_lag_seconds: float | None   (seconds since last processed webhook)
  - stripe_last_event_at:       str   | None   (ISO 8601 UTC of latest processed event)

Both are None on a cold/empty DB so freshly-deployed staging envs and the test
suite (which starts with no StripeEventId rows) don't false-alarm watchdogs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def test_healthz_returns_baseline_fields(client):
    """The pre-existing contract — must not regress."""
    r = client.get("/api/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["db"] == "ok"


def test_healthz_returns_none_lag_when_no_webhook_events(client):
    """Cold DB: no StripeEventId rows → both new fields must be None.

    This is critical — a None means "no signal", NOT "unhealthy". Any watchdog
    consuming the field must treat None as a non-event so brand-new deploys
    don't false-alarm.
    """
    r = client.get("/api/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["stripe_webhook_lag_seconds"] is None
    assert body["stripe_last_event_at"] is None


def test_healthz_reports_lag_after_processing_webhook(client, db_session):
    """After inserting a StripeEventId, the lag is a non-negative float and
    last_event_at is an ISO 8601 string within a few seconds of NOW().
    """
    from app.models import StripeEventId

    now = datetime.now(timezone.utc)
    db_session.add(
        StripeEventId(
            event_id="evt_test_healthz_recent",
            event_type="checkout.session.completed",
            processed_at=now,
            livemode=False,
        )
    )
    db_session.commit()

    r = client.get("/api/healthz")
    assert r.status_code == 200
    body = r.json()
    lag = body["stripe_webhook_lag_seconds"]
    assert isinstance(lag, (int, float)), f"lag should be numeric, got {type(lag).__name__}={lag}"
    assert lag >= 0.0
    # Generous bound — CI hosts can be slow but anything over ~30s here means
    # the route is reading a wrong column or doing wall-clock-vs-DB-time math wrong.
    assert lag < 30.0, f"lag={lag} suspiciously large for a just-inserted row"
    assert body["stripe_last_event_at"] is not None
    assert "T" in body["stripe_last_event_at"], "expected ISO 8601 format"


def test_healthz_lag_grows_with_older_events(client, db_session):
    """An older webhook event surfaces as a larger lag value — the property a
    watchdog actually keys on for the May-12-class signing-secret-drift signal.
    """
    from app.models import StripeEventId

    old = datetime.now(timezone.utc) - timedelta(seconds=3600)  # 1h ago
    db_session.add(
        StripeEventId(
            event_id="evt_test_healthz_old",
            event_type="checkout.session.completed",
            processed_at=old,
            livemode=False,
        )
    )
    db_session.commit()

    r = client.get("/api/healthz")
    assert r.status_code == 200
    body = r.json()
    lag = body["stripe_webhook_lag_seconds"]
    assert lag is not None
    # Lag must reflect the OLDEST visible event if it's the max(processed_at),
    # OR a newer one from a prior test. Either way ≥3500 if the older row is
    # the only one in this session's DB transaction.
    assert lag >= 3500.0, (
        f"lag={lag} — expected ≥3500s reflecting the 1h-old StripeEventId. "
        "If this fails, /healthz is probably reading min() instead of max(), "
        "or filtering by event_type."
    )
