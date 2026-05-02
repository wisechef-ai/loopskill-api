"""Tests for Stripe checkout + webhook subscription flow (WIS-569).

Covers:
1. POST /api/checkout/{tier} returns session URL for valid tier
2. POST /api/checkout/{tier} rejects invalid tier (400)
3. POST /api/checkout/{tier} rejects anonymous user (401)
4. Webhook: checkout.session.completed activates subscription
5. Webhook: invalid signature is rejected (400)
6. Webhook: replay (idempotency) returns 200 with already_processed
7. Webhook: customer.subscription.deleted cancels subscription
8. Webhook: checkout.session.completed skips non-subscription sessions
9. Subscription tier resolution from price ID
10. Billing /api/billing/me returns subscription state
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, StripeEventId


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_user(db: Session, email: str = "stripe@example.com", **kwargs) -> User:
    """Create and flush a User row for testing."""
    defaults = dict(
        display_name="Stripe TestUser",
        github_id=99999,
        subscription_status=None,
        subscription_tier=None,
    )
    defaults.update(kwargs)
    user = User(
        id=uuid4(),
        email=email,
        **defaults,
    )
    db.add(user)
    db.flush()
    return user


def _fake_checkout_session(session_id="cs_test_123", sub_id="sub_test_123"):
    """Build a fake Stripe Checkout Session dict."""
    return {
        "id": session_id,
        "mode": "subscription",
        "payment_status": "paid",
        "customer": "cus_test_123",
        "subscription": sub_id,
        "metadata": {},
    }


def _fake_subscription(sub_id="sub_test_123", tier="cook", price_id="price_cook_123"):
    """Build a fake Stripe Subscription dict."""
    return {
        "id": sub_id,
        "status": "active",
        "customer": "cus_test_123",
        "current_period_end": 1740000000,  # far future
        "items": {
            "data": [
                {"price": {"id": price_id, "metadata": {"tier": tier}}}
            ]
        },
        "metadata": {},
    }


def _fake_event(event_type, event_id="evt_test_001", livemode=False, **extra_data):
    """Build a fake Stripe Event dict (as returned by construct_event)."""
    return {
        "id": event_id,
        "type": event_type,
        "livemode": livemode,
        "data": {"object": extra_data},
    }


# ── Tests ────────────────────────────────────────────────────────────────


class TestCheckoutCreation:
    """Tests for POST /api/checkout/{tier}."""

    @patch("app.subscription_service.stripe")
    def test_checkout_returns_session_url(self, mock_stripe, client, db_session):
        """1. Valid tier creates a checkout session and returns {session_id, url, tier}."""
        from app.checkout_routes import get_current_user_optional
        user = _make_user(db_session, stripe_customer_id="cus_existing_001")
        mock_stripe.checkout.Session.create.return_value = {
            "id": "cs_test_xyz",
            "url": "https://checkout.stripe.com/test",
        }

        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        with patch("app.subscription_service.TIER_PRICE_IDS", {"cook": "price_test_cook", "operator": "price_test_op", "studio": "price_test_studio"}):
            try:
                resp = client.post("/api/checkout/cook")
            finally:
                client.app.dependency_overrides.pop(get_current_user_optional, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "cs_test_xyz"
        assert "checkout.stripe.com" in data["url"]
        assert data["tier"] == "cook"

    def test_checkout_rejects_invalid_tier(self, client, db_session):
        """2. Invalid tier returns 400 with helpful message."""
        from app.checkout_routes import get_current_user_optional
        user = _make_user(db_session)
        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            resp = client.post("/api/checkout/nonexistent_tier")
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)

        assert resp.status_code == 400
        assert "invalid_tier" in resp.json()["detail"]

    def test_checkout_rejects_anonymous(self, client):
        """3. Anonymous (no auth) returns 401."""
        resp = client.post("/api/checkout/cook")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "login_required"


class TestWebhookCheckoutCompleted:
    """Tests for checkout.session.completed webhook handling."""

    @patch("app.subscription_service.stripe")
    def test_checkout_completed_activates_subscription(self, mock_stripe, db_session):
        """4. checkout.session.completed sets user.subscription_status=active."""
        from app.subscription_service import handle_checkout_completed

        user = _make_user(db_session)
        session_data = _fake_checkout_session()
        session_data["metadata"]["wiserecipes_user_id"] = str(user.id)
        event = _fake_event("checkout.session.completed", **session_data)

        # Mock subscription retrieval
        fake_sub = _fake_subscription(tier="operator", price_id="price_op_123")
        mock_stripe.Subscription.retrieve.return_value = fake_sub

        result = handle_checkout_completed(event, db_session)
        assert result["processed"] == "checkout.session.completed"
        db_session.refresh(user)
        assert user.subscription_status == "active"
        assert user.subscription_tier == "operator"

    def test_checkout_completed_skips_non_subscription(self, db_session):
        """8. Non-subscription sessions (e.g., payment) are skipped."""
        from app.subscription_service import handle_checkout_completed

        session_data = _fake_checkout_session()
        session_data["mode"] = "payment"  # not subscription
        event = _fake_event("checkout.session.completed", **session_data)

        result = handle_checkout_completed(event, db_session)
        assert result.get("skipped") == "non-subscription session"


class TestWebhookSignatureVerification:
    """Tests for webhook signature verification."""

    @patch("app.subscription_service.stripe")
    def test_invalid_signature_rejected(self, mock_stripe, client):
        """5. Invalid webhook signature returns 400."""
        import stripe as real_stripe
        mock_stripe.error.SignatureVerificationError = (
            real_stripe.error.SignatureVerificationError
        )
        mock_stripe.Webhook.construct_event.side_effect = (
            real_stripe.error.SignatureVerificationError("bad sig", "payload", "sig")
        )

        resp = client.post(
            "/api/stripe/webhook",
            content=b'{"type":"test"}',
            headers={"stripe-signature": "bad_signature"},
        )
        assert resp.status_code == 400
        assert "Invalid signature" in resp.json()["detail"]


class TestWebhookIdempotency:
    """Tests for webhook event deduplication."""

    def test_replay_returns_already_processed(self, db_session):
        """6. Replaying the same event_id returns already_processed=True."""
        from app.subscription_service import record_event_or_skip

        event = {"id": "evt_unique_001", "type": "test", "livemode": False}

        # First call — should be new
        assert record_event_or_skip(event, db_session) is True

        # Second call — same event_id — should be a replay
        assert record_event_or_skip(event, db_session) is False


class TestSubscriptionDeleted:
    """Tests for customer.subscription.deleted webhook."""

    def test_subscription_deleted_clears_user_state(self, db_session):
        """7. Subscription deleted sets status=canceled, clears tier/ID."""
        from app.subscription_service import handle_subscription_event

        user = _make_user(
            db_session,
            subscription_status="active",
            subscription_tier="cook",
            subscription_id="sub_active_001",
        )

        sub_data = _fake_subscription(sub_id="sub_active_001", tier="cook")
        sub_data["metadata"]["wiserecipes_user_id"] = str(user.id)
        event = _fake_event("customer.subscription.deleted", **sub_data)

        result = handle_subscription_event(event, db_session)
        assert result["processed"] == "customer.subscription.deleted"
        db_session.refresh(user)
        assert user.subscription_status == "canceled"
        assert user.subscription_tier is None
        assert user.subscription_id is None


class TestTierResolution:
    """Tests for subscription tier resolution from price ID."""

    @patch("app.subscription_service.stripe")
    def test_tier_resolved_from_price_metadata(self, mock_stripe, db_session):
        """9. Tier is correctly resolved from price metadata."""
        from app.subscription_service import handle_checkout_completed

        user = _make_user(db_session)
        session_data = _fake_checkout_session()
        session_data["metadata"]["wiserecipes_user_id"] = str(user.id)
        event = _fake_event("checkout.session.completed", **session_data)

        fake_sub = _fake_subscription(tier="studio", price_id="price_studio_24900")
        mock_stripe.Subscription.retrieve.return_value = fake_sub

        handle_checkout_completed(event, db_session)
        db_session.refresh(user)
        assert user.subscription_tier == "studio"


class TestBillingMe:
    """Tests for GET /api/billing/me."""

    def test_billing_me_returns_subscription_state(self, client, db_session):
        """10. /api/billing/me returns current subscription state for authed user."""
        from app.checkout_routes import get_current_user_optional
        user = _make_user(
            db_session,
            subscription_status="active",
            subscription_tier="cook",
            stripe_customer_id="cus_billing_test",
            subscription_id="sub_billing_test",
        )

        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            resp = client.get("/api/billing/me")
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["subscription_status"] == "active"
        assert data["subscription_tier"] == "cook"
        assert data["stripe_customer_id"] == "cus_billing_test"

    def test_billing_me_rejects_anonymous(self, client):
        """GET /api/billing/me returns 401 for anonymous."""
        resp = client.get("/api/billing/me")
        assert resp.status_code == 401
