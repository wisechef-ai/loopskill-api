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


# ────────────────────────────────────────────────────────────────────────
# fleet-heal-0524 t_a488bb1d — stripe_webhook_lag_label
# ────────────────────────────────────────────────────────────────────────
# The label disambiguates a quiet-MRR deployment (Scenario A from
# t_68a9e9b9 triage) from a real webhook-pipeline drift (May-12 incident
# class) so future fleet-heal sweeps do not flag healthy quiet traffic.


def test_healthz_label_is_none_when_no_events(client):
    """Cold DB — no lag, no label. The label is purely a function of lag."""
    r = client.get("/api/healthz")
    body = r.json()
    assert body["stripe_webhook_lag_seconds"] is None
    assert body["stripe_webhook_lag_label"] is None


def test_healthz_label_is_none_when_lag_below_threshold(client, db_session):
    """Recent webhook event → lag well below 1h threshold → label silent."""
    from app.models import StripeEventId

    now = datetime.now(timezone.utc)
    db_session.add(
        StripeEventId(
            event_id="evt_recent_no_label",
            event_type="checkout.session.completed",
            processed_at=now,
            livemode=False,
        )
    )
    db_session.commit()
    r = client.get("/api/healthz")
    body = r.json()
    assert body["stripe_webhook_lag_seconds"] is not None
    assert body["stripe_webhook_lag_seconds"] < 30
    assert (
        body["stripe_webhook_lag_label"] is None
    ), "healthy lag must not carry a label — labels are only set on drift suspicion"


def test_healthz_label_no_qualifying_traffic_on_zero_mrr(client, db_session):
    """Lag above threshold + zero paid signups in window → 'no_qualifying_traffic'.

    This is the Scenario A case fleet-heal-0524 false-flagged. The label tells
    a future sweep that the silence is real-world silence, not drift.
    """
    from app.models import StripeEventId

    old = datetime.now(timezone.utc) - timedelta(seconds=7200)  # 2h ago
    db_session.add(
        StripeEventId(
            event_id="evt_old_quiet",
            event_type="checkout.session.completed",
            processed_at=old,
            livemode=False,
        )
    )
    db_session.commit()
    r = client.get("/api/healthz")
    body = r.json()
    assert body["stripe_webhook_lag_seconds"] >= 3600
    assert body["stripe_webhook_lag_label"] == "no_qualifying_traffic", (
        f"expected no_qualifying_traffic with no paid User rows, got " f"{body['stripe_webhook_lag_label']!r}"
    )


def test_healthz_label_drift_suspected_when_paid_traffic_exists(client, db_session):
    """Lag above threshold + a recent paid signup → 'drift_suspected'.

    A new paid User in the 30-day window is unambiguous evidence that
    subscribed-type webhook traffic SHOULD have arrived. Lag above 1h means
    it didn't — investigate signing-secret / endpoint / RBAC drift.
    """
    from app.models import StripeEventId, User
    from uuid import uuid4

    old = datetime.now(timezone.utc) - timedelta(seconds=7200)
    db_session.add(
        StripeEventId(
            event_id="evt_old_with_paid_traffic",
            event_type="checkout.session.completed",
            processed_at=old,
            livemode=False,
        )
    )
    # Recent paid signup
    db_session.add(
        User(
            id=uuid4(),
            display_name="Paying Cook",
            email="cook@example.com",
            subscription_tier="cook",
            subscription_status="active",
        )
    )
    db_session.commit()
    r = client.get("/api/healthz")
    body = r.json()
    assert body["stripe_webhook_lag_seconds"] >= 3600
    assert body["stripe_webhook_lag_label"] == "drift_suspected", (
        f"expected drift_suspected with a recent paid User row, got " f"{body['stripe_webhook_lag_label']!r}"
    )
