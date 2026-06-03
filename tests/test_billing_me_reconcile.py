"""Phase 2 — /api/billing/me server-side reconciliation tests.

Verifies that the billing/me endpoint reconciles the user's subscription state
from Stripe when the DB is stale (race condition after checkout, before webhook).

Test cases:
1. User with active Stripe sub but tier=NULL → reconcile triggered, tier set in DB.
2. User with sub already in sync (tier set, status active) → no Stripe call made.
3. Stripe API raises → endpoint still returns 200 with stale DB state (no 5xx).
4. Hammering rate-limit: 2 calls within 5s → second call must NOT hit Stripe.
5. User without stripe_customer_id → no Stripe call, plain DB read.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.models import Base, User


# ── DB fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
    """Per-test transactional session — rolls back after each test."""
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()

    # Re-issue SAVEPOINT after each commit so test isolation holds.
    nested = connection.begin_nested()

    @sa_event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session, txn):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ── Settings + app fixtures ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def configured_prices(monkeypatch):
    """Ensure consistent price IDs and secret key across all tests."""
    from app import subscription_service as ss
    monkeypatch.setattr(settings, "STRIPE_PRICE_COOK", "price_test_cook")
    monkeypatch.setattr(settings, "STRIPE_PRICE_STUDIO", "price_test_studio")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(ss, "TIER_PRICE_IDS", {
        "pro": "price_test_cook",
        "pro_plus": "price_test_studio",
    })


def _build_app(db: Session, user: User) -> TestClient:
    """Return a TestClient where get_current_user_optional → user."""
    from app.checkout_routes import router as checkout_router
    from app import auth_routes

    app = FastAPI()

    def _override_db():
        yield db

    def _override_user():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[auth_routes.get_current_user_optional] = _override_user
    app.include_router(checkout_router)
    return TestClient(app)


def _make_user(db: Session, **kwargs) -> User:
    """Helper: create and persist a User row."""
    defaults = dict(
        id=uuid.uuid4(),
        github_id=100_000 + int(uuid.uuid4().int) % 900_000,
        email=f"test-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Reconcile Test User",
        stripe_customer_id=None,
        subscription_tier=None,
        subscription_status=None,
        subscription_id=None,
    )
    defaults.update(kwargs)
    user = User(**defaults)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _fake_stripe_sub(price_id: str = "price_test_studio", tier_meta: str = "pro_plus") -> dict:
    """Build a minimal Stripe Subscription dict."""
    return {
        "id": f"sub_test_{uuid.uuid4().hex[:8]}",
        "status": "active",
        "current_period_end": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
        "items": {"data": [{"price": {"id": price_id, "metadata": {"tier": tier_meta}}}]},
        "customer": "cus_test_xyz",
        "metadata": {},
    }


# ── Test 1: tier=NULL with active Stripe sub → reconcile triggers ─────────────

def test_tier_null_active_sub_triggers_reconcile(db):
    """Gate 1: user has stripe_customer_id + active sub but tier=NULL → reconcile sets tier."""
    from app.checkout_routes import _reconcile_last_attempt

    user = _make_user(db, stripe_customer_id="cus_tier_null")
    # Ensure no cooldown is in effect
    _reconcile_last_attempt.pop(str(user.id), None)

    fake_sub = _fake_stripe_sub()
    client = _build_app(db, user)

    with patch("stripe.Subscription.list") as mock_list:
        mock_list.return_value = {"data": [fake_sub]}
        resp = client.get("/api/billing/me")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subscription_tier"] == "pro_plus", f"Expected 'pro_plus', got {body['subscription_tier']!r}"
    assert body["subscription_status"] == "active"

    # Verify DB row was actually updated
    db.expire_all()
    refreshed = db.query(User).filter(User.id == user.id).first()
    assert refreshed.subscription_tier == "pro_plus"
    assert refreshed.subscription_status == "active"

    mock_list.assert_called_once()


# ── Test 2: sub already in sync → no Stripe call ──────────────────────────────

def test_already_synced_no_stripe_call(db):
    """Gate 2: user with active tier + active status → no Stripe call made."""
    from app.checkout_routes import _reconcile_last_attempt

    user = _make_user(
        db,
        stripe_customer_id="cus_already_synced",
        subscription_tier="pro",
        subscription_status="active",
        subscription_id="sub_already_synced",
    )
    _reconcile_last_attempt.pop(str(user.id), None)

    client = _build_app(db, user)

    with patch("stripe.Subscription.list") as mock_list:
        resp = client.get("/api/billing/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription_tier"] == "pro"
    mock_list.assert_not_called()


# ── Test 3: Stripe raises → 200 with stale state, no 5xx ─────────────────────

def test_stripe_api_error_returns_stale_state(db):
    """Gate 3: if Stripe raises, endpoint still returns 200 with stale DB data."""
    from app.checkout_routes import _reconcile_last_attempt

    user = _make_user(db, stripe_customer_id="cus_stripe_error")
    _reconcile_last_attempt.pop(str(user.id), None)

    client = _build_app(db, user)

    with patch("stripe.Subscription.list", side_effect=Exception("Stripe is down")):
        resp = client.get("/api/billing/me")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    # Stale DB state: both None
    assert body["subscription_tier"] is None
    assert body["subscription_status"] is None


# ── Test 4: hammering rate-limit — 2 calls within 5s → no second Stripe hit ──

def test_hammering_rate_limit(db):
    """Gate 4: two rapid requests within 5s window → only one Stripe call."""
    from app.checkout_routes import _reconcile_last_attempt

    user = _make_user(db, stripe_customer_id="cus_hammer_test")
    _reconcile_last_attempt.pop(str(user.id), None)

    fake_sub = _fake_stripe_sub()
    client = _build_app(db, user)

    call_count = 0

    def _fake_list(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"data": [fake_sub]}

    with patch("stripe.Subscription.list", side_effect=_fake_list):
        resp1 = client.get("/api/billing/me")
        # Immediately hammer a second call — should hit the cooldown
        resp2 = client.get("/api/billing/me")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert call_count == 1, (
        f"Stripe was called {call_count} time(s); expected exactly 1 (cooldown should block second)"
    )


# ── Test 5: no stripe_customer_id → plain DB read, no Stripe call ─────────────

def test_no_stripe_customer_id_no_call(db):
    """Gate 5: user without stripe_customer_id → no Stripe call, returns DB data."""
    from app.checkout_routes import _reconcile_last_attempt

    user = _make_user(db)  # stripe_customer_id=None by default
    _reconcile_last_attempt.pop(str(user.id), None)

    client = _build_app(db, user)

    with patch("stripe.Subscription.list") as mock_list:
        resp = client.get("/api/billing/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stripe_customer_id"] is None
    mock_list.assert_not_called()


# ── Test 6: cookbook_limit reflects the tier SSOT (loopclose_3005 Phase X) ────


@pytest.mark.parametrize(
    "tier,expected_limit",
    [("pro", 10), ("pro_plus", 200), (None, 1)],
)
def test_billing_me_returns_cookbook_limit_from_ssot(db, tier, expected_limit):
    """Phase X: /api/billing/me must expose cookbook_limit read from the
    config/tiers.yaml SSOT so the portal library copy never drifts. Free
    (tier=None) → 1 (evergreen_0206 Phase A on-ramp), Pro → 10, Pro+ → 200."""
    from app.checkout_routes import _reconcile_last_attempt

    kwargs = {}
    if tier is not None:
        kwargs = {"subscription_tier": tier, "subscription_status": "active"}
    user = _make_user(db, **kwargs)
    _reconcile_last_attempt.pop(str(user.id), None)

    client = _build_app(db, user)
    with patch("stripe.Subscription.list") as mock_list:
        resp = client.get("/api/billing/me")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "cookbook_limit" in body, "billing/me must expose cookbook_limit"
    assert body["cookbook_limit"] == expected_limit, (
        f"tier={tier!r} expected cookbook_limit={expected_limit}, got {body['cookbook_limit']}"
    )
    mock_list.assert_not_called()
