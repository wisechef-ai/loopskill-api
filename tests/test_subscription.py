"""Tests for WIS-569 — Stripe subscription checkout, webhook, idempotency.

Acceptance gates covered:
1. POST /api/checkout/{tier} creates a session and returns checkout URL — auth'd
2. POST /api/checkout/{tier} returns 401 for anonymous
3. POST /api/checkout/{tier} returns 400 for unknown tier
4. POST /api/stripe/webhook with bad signature returns 400
5. checkout.session.completed marks subscription_status=active
6. customer.subscription.updated syncs status + period_end
7. customer.subscription.deleted clears tier + sets status=canceled
8. Webhook idempotency: replay returns already_processed=True with no side effects
9. Refund (charge.refunded) does NOT cancel subscription

Uses in-memory SQLite + dependency_overrides — no prod DB touched.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.models import Base, StripeEventId, User


# ── DB fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
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
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ── Settings fixtures ────────────────────────────────────────────────────

@pytest.fixture
def configured_prices(monkeypatch):
    """Test price IDs in settings + subscription_service.TIER_PRICE_IDS.

    Phase A (stabilization_2605): operator tier retired; only cook + studio.
    """
    from app import subscription_service as ss
    monkeypatch.setattr(settings, "STRIPE_PRICE_COOK", "price_test_cook")
    monkeypatch.setattr(settings, "STRIPE_PRICE_STUDIO", "price_test_studio")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE", "https://recipes.test/")
    monkeypatch.setattr(ss, "TIER_PRICE_IDS", {
        "cook": "price_test_cook",
        "studio": "price_test_studio",
    })
    yield


# ── App / Client fixtures ────────────────────────────────────────────────

def _build_test_app(db: Session) -> FastAPI:
    """Build a FastAPI app with only the routers we need + db override + auth bypass.

    Skips middleware (no API key required for this in-memory app).
    """
    from app.checkout_routes import router as checkout_router
    from app.creator_routes import router as creator_router

    app = FastAPI()
    def _override_get_db():
        try:
            yield db
        finally:
            pass
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(checkout_router)
    app.include_router(creator_router)
    return app


@pytest.fixture
def test_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        github_id=999_001 + int(uuid.uuid4().int) % 1_000_000,
        email=f"wis569-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="WIS-569 Test User",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def authed_client(db, test_user, configured_prices) -> TestClient:
    """TestClient where get_current_user_optional returns test_user."""
    from app import auth_routes

    app = _build_test_app(db)
    # Patch the dependency to return our test user
    def _override_user():
        return test_user
    app.dependency_overrides[auth_routes.get_current_user_optional] = _override_user
    return TestClient(app)


@pytest.fixture
def anon_client(db, configured_prices) -> TestClient:
    """TestClient with no authenticated user."""
    from app import auth_routes
    app = _build_test_app(db)
    def _override_user():
        return None
    app.dependency_overrides[auth_routes.get_current_user_optional] = _override_user
    return TestClient(app)


@pytest.fixture
def webhook_client(db, configured_prices) -> TestClient:
    """TestClient for /api/stripe/webhook (no auth needed, uses signature)."""
    return TestClient(_build_test_app(db))


# ── Tests: POST /api/checkout/{tier} ─────────────────────────────────────

@pytest.mark.parametrize("tier", ["cook", "studio"])
def test_checkout_creates_session_for_authenticated_user(authed_client, tier):
    """Gate 1: authenticated POST /api/checkout/{tier} creates a Stripe session."""
    fake_session = {
        "id": f"cs_test_{tier}_abc",
        "url": f"https://checkout.stripe.com/c/pay/cs_test_{tier}_abc",
    }
    fake_customer = {"id": f"cus_test_{tier}"}

    with patch("stripe.checkout.Session.create", return_value=fake_session) as session_create, \
         patch("stripe.Customer.create", return_value=fake_customer):
        resp = authed_client.post(f"/api/checkout/{tier}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == fake_session["id"]
    assert body["url"].startswith("https://checkout.stripe.com/")
    assert body["tier"] == tier

    kwargs = session_create.call_args.kwargs
    assert kwargs["mode"] == "subscription"
    assert kwargs["line_items"] == [{"price": f"price_test_{tier}", "quantity": 1}]
    assert kwargs["automatic_tax"] == {"enabled": True}
    assert kwargs["tax_id_collection"] == {"enabled": True}
    assert kwargs["billing_address_collection"] == "required"
    assert kwargs["metadata"]["tier"] == tier


def test_checkout_anonymous_returns_401(anon_client):
    """Gate 2: anonymous users get 401."""
    resp = anon_client.post("/api/checkout/cook")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "login_required"


def test_checkout_unknown_tier_returns_400(authed_client):
    """Gate 3: invalid tier returns 400."""
    resp = authed_client.post("/api/checkout/diamond")
    assert resp.status_code == 400
    assert "invalid_tier" in resp.json()["detail"]


# ── Tests: POST /api/stripe/webhook signature ────────────────────────────

def test_webhook_bad_signature_returns_400(webhook_client):
    """Gate 4: webhook with bad signature returns 400."""
    resp = webhook_client.post(
        "/api/stripe/webhook",
        content=b'{"id":"evt_test","type":"checkout.session.completed"}',
        headers={"stripe-signature": "t=1,v1=bogus"},
    )
    assert resp.status_code == 400


# ── Helpers for webhook tests ────────────────────────────────────────────

def _post_event(client: TestClient, event: dict):
    payload = json.dumps(event).encode()
    with patch("app.creator_routes.verify_webhook_signature", return_value=event):
        return client.post(
            "/api/stripe/webhook",
            content=payload,
            headers={"stripe-signature": "t=1,v1=test"},
        )


# ── Tests: subscription lifecycle ────────────────────────────────────────

def test_checkout_completed_marks_subscription_active(test_user, db, webhook_client):
    """Gate 5: checkout.session.completed activates user's subscription."""
    sub_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    fake_subscription = {
        "id": sub_id,
        "status": "active",
        "current_period_end": period_end,
        "items": {"data": [{
            "price": {"id": "price_test_studio", "metadata": {"tier": "studio"}},
        }]},
        "metadata": {"wiserecipes_user_id": str(test_user.id)},
        "customer": "cus_test_completed",
    }
    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": {
            "id": "cs_test_completed",
            "object": "checkout.session",
            "mode": "subscription",
            "payment_status": "paid",
            "customer": "cus_test_completed",
            "subscription": sub_id,
            "metadata": {"wiserecipes_user_id": str(test_user.id), "tier": "studio"},
        }},
    }

    with patch("stripe.Subscription.retrieve", return_value=fake_subscription):
        resp = _post_event(webhook_client, event)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["received"] is True
    assert body["processed"] == "checkout.session.completed"

    db.expire_all()
    user = db.query(User).filter(User.id == test_user.id).first()
    assert user.subscription_status == "active"
    assert user.subscription_tier == "studio"
    assert user.subscription_id == sub_id
    assert user.subscription_current_period_end is not None


def test_subscription_updated_syncs_state(test_user, db, webhook_client):
    """Gate 6: customer.subscription.updated syncs state."""
    sub_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    test_user.stripe_customer_id = "cus_test_updated"
    db.commit()

    period_end = int((datetime.now(timezone.utc) + timedelta(days=15)).timestamp())
    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "customer.subscription.updated",
        "livemode": False,
        "data": {"object": {
            "id": sub_id,
            "status": "past_due",
            "current_period_end": period_end,
            "customer": "cus_test_updated",
            "metadata": {"wiserecipes_user_id": str(test_user.id), "tier": "cook"},
            "items": {"data": [{
                "price": {"id": "price_test_cook", "metadata": {"tier": "cook"}},
            }]},
        }},
    }
    resp = _post_event(webhook_client, event)
    assert resp.status_code == 200

    db.expire_all()
    user = db.query(User).filter(User.id == test_user.id).first()
    assert user.subscription_status == "past_due"
    assert user.subscription_tier == "cook"
    assert user.subscription_id == sub_id


def test_subscription_deleted_clears_state(test_user, db, webhook_client):
    """Gate 7: customer.subscription.deleted clears tier + cancels."""
    test_user.stripe_customer_id = "cus_test_deleted"
    test_user.subscription_id = "sub_to_be_deleted"
    test_user.subscription_status = "active"
    test_user.subscription_tier = "studio"
    db.commit()

    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {
            "id": "sub_to_be_deleted",
            "status": "canceled",
            "customer": "cus_test_deleted",
            "metadata": {"wiserecipes_user_id": str(test_user.id)},
        }},
    }
    resp = _post_event(webhook_client, event)
    assert resp.status_code == 200

    db.expire_all()
    user = db.query(User).filter(User.id == test_user.id).first()
    assert user.subscription_status == "canceled"
    assert user.subscription_id is None
    assert user.subscription_tier is None


# ── Tests: idempotency ──────────────────────────────────────────────────

def test_webhook_replay_is_no_op(test_user, db, webhook_client):
    """Gate 8: replaying same event_id returns already_processed=True, no side effects."""
    event_id = f"evt_replay_{uuid.uuid4().hex}"
    test_user.stripe_customer_id = "cus_test_replay"
    db.commit()

    period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    event = {
        "id": event_id,
        "type": "customer.subscription.updated",
        "livemode": False,
        "data": {"object": {
            "id": "sub_replay",
            "status": "active",
            "current_period_end": period_end,
            "customer": "cus_test_replay",
            "metadata": {"wiserecipes_user_id": str(test_user.id), "tier": "cook"},
            "items": {"data": [{
                "price": {"id": "price_test_cook", "metadata": {"tier": "cook"}},
            }]},
        }},
    }

    resp1 = _post_event(webhook_client, event)
    assert resp1.status_code == 200
    assert resp1.json().get("already_processed") is None
    db.expire_all()
    user_id = test_user.id
    user = db.query(User).filter(User.id == user_id).first()
    assert user.subscription_status == "active"

    # Set distinguishable value, replay must NOT change it
    user.subscription_status = "user_set_value"
    db.commit()

    resp2 = _post_event(webhook_client, event)
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["already_processed"] is True
    assert body["event_id"] == event_id

    db.expire_all()
    user = db.query(User).filter(User.id == user_id).first()
    assert user is not None
    assert user.subscription_status == "user_set_value"


# ── Tests: refund handling ──────────────────────────────────────────────

def test_refund_does_not_cancel_subscription(test_user, db, webhook_client):
    """Gate 9: charge.refunded is a no-op for subscription state."""
    test_user.stripe_customer_id = "cus_test_refund"
    test_user.subscription_id = "sub_active_during_refund"
    test_user.subscription_status = "active"
    test_user.subscription_tier = "studio"
    db.commit()

    event = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "charge.refunded",
        "livemode": False,
        "data": {"object": {
            "id": "ch_refund_test",
            "amount_refunded": 50,
            "customer": "cus_test_refund",
        }},
    }
    resp = _post_event(webhook_client, event)
    assert resp.status_code == 200
    assert resp.json().get("received") is True

    db.expire_all()
    user = db.query(User).filter(User.id == test_user.id).first()
    assert user.subscription_status == "active"
    assert user.subscription_tier == "studio"


# ── Tests: GET /api/billing/me ──────────────────────────────────────────

def test_billing_me_returns_subscription_state(test_user, db, authed_client):
    """Sanity: GET /api/billing/me reflects DB state."""
    test_user.subscription_status = "active"
    test_user.subscription_tier = "cook"
    test_user.stripe_customer_id = "cus_billing_me"
    db.commit()

    resp = authed_client.get("/api/billing/me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(test_user.id)
    assert body["subscription_status"] == "active"
    assert body["subscription_tier"] == "cook"
    assert body["stripe_customer_id"] == "cus_billing_me"


def test_billing_me_anonymous_returns_401(anon_client):
    """Anonymous /api/billing/me returns 401."""
    resp = anon_client.get("/api/billing/me")
    assert resp.status_code == 401
