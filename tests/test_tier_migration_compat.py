"""Tests for RCP-INCIDENT-2026-05-11 Phase 5: cook→pro, operator→pro_plus migration compat.

Covers:
1. _apply_subscription_state with price.metadata.tier='studio'/'operator'/'cook' → canonical shim
2. downgrade_studio_to_cook still works (delegates to pro_plus→pro)
3. downgrade_operator_to_cook still works (delegates to pro_plus→pro)
4. tier_labels._is_pro_plus_tier('studio'/'operator') == True (legacy accepted)
5. tier_labels._is_pro_plus_tier('pro_plus') == True (canonical)
6. tier_labels._is_pro_plus_tier('cook') == False
7. Checkout URL alias rewriting: /api/checkout/cook → pro, /api/checkout/operator → pro_plus
"""
from __future__ import annotations

import uuid
import warnings
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, User


# ── DB fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
    def _pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Session:
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


def _make_user(db, tier="pro_plus", status="active", sub_id="sub_test_001") -> User:
    user = User(
        id=uuid.uuid4(),
        github_id=abs(hash(uuid.uuid4().hex)) % 10_000_000,
        email=f"compat-{uuid.uuid4().hex[:6]}@test.wisechef.ai",
        display_name="Compat Test User",
        stripe_customer_id="cus_compat_test",
        subscription_id=sub_id,
        subscription_status=status,
        subscription_tier=tier,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── 1. _apply_subscription_state shim tests ───────────────────────────────

class TestApplySubscriptionStateShim:
    def test_studio_tier_in_price_metadata_normalised_to_pro_plus(self, db):
        """price.metadata.tier='studio' → user.subscription_tier='pro_plus' via shim."""
        import importlib
        from app import subscription_service as ss
        ss = importlib.reload(ss)

        user = _make_user(db, tier=None, status=None, sub_id="sub_shim_studio")
        user.subscription_tier = None
        db.commit()

        period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        fake_sub = {
            "id": "sub_shim_studio",
            "status": "active",
            "current_period_end": period_end,
            "items": {"data": [
                {"price": {"id": "price_studio_legacy", "metadata": {"tier": "studio"}}},
            ]},
            "metadata": {"wiserecipes_user_id": str(user.id)},
        }

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_x", "pro_plus": "price_pp_x"}):
            ss._apply_subscription_state(user, fake_sub, db)

        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "pro_plus", (
            f"Expected 'pro_plus', got {fresh.subscription_tier!r} — shim not working"
        )

    def test_operator_tier_in_price_metadata_normalised_to_pro_plus(self, db):
        """price.metadata.tier='operator' → user.subscription_tier='pro_plus' via Phase 5 shim."""
        import importlib
        from app import subscription_service as ss
        ss = importlib.reload(ss)

        user = _make_user(db, tier=None, status=None, sub_id="sub_shim_operator")
        user.subscription_tier = None
        db.commit()

        period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        fake_sub = {
            "id": "sub_shim_operator",
            "status": "active",
            "current_period_end": period_end,
            "items": {"data": [
                {"price": {"id": "price_op_legacy", "metadata": {"tier": "operator"}}},
            ]},
            "metadata": {"wiserecipes_user_id": str(user.id)},
        }

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_x", "pro_plus": "price_pp_x"}):
            ss._apply_subscription_state(user, fake_sub, db)

        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "pro_plus"

    def test_cook_tier_in_price_metadata_normalised_to_pro(self, db):
        """price.metadata.tier='cook' → user.subscription_tier='pro' via Phase 5 shim."""
        import importlib
        from app import subscription_service as ss
        ss = importlib.reload(ss)

        user = _make_user(db, tier=None, status=None, sub_id="sub_shim_cook")
        user.subscription_tier = None
        db.commit()

        period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        fake_sub = {
            "id": "sub_shim_cook",
            "status": "active",
            "current_period_end": period_end,
            "items": {"data": [
                {"price": {"id": "price_cook_legacy", "metadata": {"tier": "cook"}}},
            ]},
            "metadata": {"wiserecipes_user_id": str(user.id)},
        }

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_x", "pro_plus": "price_pp_x"}):
            ss._apply_subscription_state(user, fake_sub, db)

        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "pro"

    def test_canonical_pro_plus_in_price_id_reverse_lookup(self, db):
        """If tier comes from price-id reverse-lookup and the key is 'pro_plus', it stays."""
        import importlib
        from app import subscription_service as ss
        ss = importlib.reload(ss)

        user = _make_user(db, tier=None, status=None, sub_id="sub_rev_lookup")
        user.subscription_tier = None
        db.commit()

        period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        fake_sub = {
            "id": "sub_rev_lookup",
            "status": "active",
            "current_period_end": period_end,
            "items": {"data": [
                {"price": {"id": "price_pp_match", "metadata": {}}},
            ]},
            "metadata": {"wiserecipes_user_id": str(user.id)},
        }

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_x", "pro_plus": "price_pp_match"}):
            ss._apply_subscription_state(user, fake_sub, db)

        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "pro_plus"


# ── 2. Downgrade wrapper tests ────────────────────────────────────────────

class TestDowngradeWrappers:
    def test_downgrade_studio_to_cook_delegates_for_pro_plus_user(self, db):
        """downgrade_studio_to_cook() with a 'pro_plus' user → delegates, returns pro."""
        from app import subscription_service as ss

        user = _make_user(db, tier="pro_plus", sub_id="sub_downgrade_studio_w")

        fake_sub = {"id": user.subscription_id, "items": {"data": [{"id": "si_001"}]}}
        fake_modified = {"id": user.subscription_id, "status": "active"}

        with patch("stripe.Subscription.retrieve", return_value=fake_sub), \
             patch("stripe.Subscription.modify", return_value=fake_modified), \
             patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_test", "pro_plus": "price_pp_test"}):
            result = ss.downgrade_studio_to_cook(user, db)

        assert result["tier"] == "pro"
        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "pro"

    def test_downgrade_operator_to_cook_delegates_for_pro_plus_user(self, db):
        """downgrade_operator_to_cook() with a 'pro_plus' user → delegates, returns pro."""
        from app import subscription_service as ss

        user = _make_user(db, tier="pro_plus", sub_id="sub_downgrade_op_w")

        fake_sub = {"id": user.subscription_id, "items": {"data": [{"id": "si_002"}]}}
        fake_modified = {"id": user.subscription_id, "status": "active"}

        with patch("stripe.Subscription.retrieve", return_value=fake_sub), \
             patch("stripe.Subscription.modify", return_value=fake_modified), \
             patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_test", "pro_plus": "price_pp_test"}):
            result = ss.downgrade_operator_to_cook(user, db)

        assert result["tier"] == "pro"

    def test_downgrade_studio_accepts_legacy_operator_tier(self, db):
        """downgrade_studio_to_cook() with a legacy 'operator' user → normalised, succeeds."""
        from app import subscription_service as ss

        user = _make_user(db, tier="operator", sub_id="sub_downgrade_op_legacy")

        fake_sub = {"id": user.subscription_id, "items": {"data": [{"id": "si_003"}]}}
        fake_modified = {"id": user.subscription_id, "status": "active"}

        with patch("stripe.Subscription.retrieve", return_value=fake_sub), \
             patch("stripe.Subscription.modify", return_value=fake_modified), \
             patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_test", "pro_plus": "price_pp_test"}):
            result = ss.downgrade_studio_to_cook(user, db)

        assert result["tier"] == "pro"

    def test_downgrade_rejects_pro_user(self, db):
        """downgrade_pro_plus_to_pro raises for a pro user."""
        from app.subscription_service import downgrade_pro_plus_to_pro, SubscriptionError

        user = _make_user(db, tier="pro", sub_id="sub_pro_user")
        with pytest.raises(SubscriptionError, match="not_pro_plus"):
            downgrade_pro_plus_to_pro(user, db)

    def test_downgrade_rejects_legacy_cook_user(self, db):
        """downgrade_pro_plus_to_pro raises for a legacy 'cook' user (normalises to 'pro')."""
        from app.subscription_service import downgrade_pro_plus_to_pro, SubscriptionError

        user = _make_user(db, tier="cook", sub_id="sub_cook_user")
        with pytest.raises(SubscriptionError, match="not_pro_plus"):
            downgrade_pro_plus_to_pro(user, db)


# ── 3-7. tier_labels helpers ──────────────────────────────────────────────

class TestTierLabelHelpers:
    def test_is_pro_plus_tier_studio(self):
        """Legacy 'studio' slug accepted by _is_pro_plus_tier."""
        from app.tier_labels import _is_pro_plus_tier
        assert _is_pro_plus_tier("studio") is True

    def test_is_pro_plus_tier_operator(self):
        """Legacy 'operator' slug accepted by _is_pro_plus_tier."""
        from app.tier_labels import _is_pro_plus_tier
        assert _is_pro_plus_tier("operator") is True

    def test_is_pro_plus_tier_pro_plus(self):
        """Canonical 'pro_plus' slug accepted."""
        from app.tier_labels import _is_pro_plus_tier
        assert _is_pro_plus_tier("pro_plus") is True

    def test_is_pro_plus_tier_cook(self):
        """'cook' is not pro_plus tier."""
        from app.tier_labels import _is_pro_plus_tier
        assert _is_pro_plus_tier("cook") is False

    def test_is_pro_plus_tier_pro(self):
        """'pro' is not pro_plus tier."""
        from app.tier_labels import _is_pro_plus_tier
        assert _is_pro_plus_tier("pro") is False

    def test_is_pro_plus_tier_none(self):
        """None is not pro_plus tier."""
        from app.tier_labels import _is_pro_plus_tier
        assert _is_pro_plus_tier(None) is False

    def test_is_pro_plus_tier_free(self):
        """'free' is not pro_plus tier."""
        from app.tier_labels import _is_pro_plus_tier
        assert _is_pro_plus_tier("free") is False

    def test_is_operator_tier_still_works_as_wrapper(self):
        """_is_operator_tier wrapper still delegates correctly."""
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier("pro_plus") is True
        assert _is_operator_tier("operator") is True
        assert _is_operator_tier("pro") is False

    def test_is_paid_tier_pro(self):
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("pro") is True

    def test_is_paid_tier_pro_plus(self):
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("pro_plus") is True

    def test_is_paid_tier_legacy_cook(self):
        """Legacy 'cook' slug accepted as paid tier."""
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("cook") is True

    def test_is_paid_tier_legacy_operator(self):
        """Legacy 'operator' slug accepted as paid tier."""
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("operator") is True

    def test_is_paid_tier_studio_legacy(self):
        """Legacy 'studio' slug accepted as paid tier."""
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("studio") is True

    def test_is_paid_tier_free(self):
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("free") is False

    def test_is_paid_tier_none(self):
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier(None) is False

    def test_display_label_studio_maps_to_pro_plus(self):
        """display_label('studio') returns 'Pro+' (via legacy slug mapping)."""
        from app.tier_labels import display_label
        assert display_label("studio") == "Pro+"

    def test_display_label_operator_maps_to_pro_plus(self):
        from app.tier_labels import display_label
        assert display_label("operator") == "Pro+"

    def test_display_label_pro_plus_maps_to_pro_plus(self):
        from app.tier_labels import display_label
        assert display_label("pro_plus") == "Pro+"

    def test_display_label_pro_maps_to_pro(self):
        from app.tier_labels import display_label
        assert display_label("pro") == "Pro"

    def test_display_label_cook_maps_to_pro(self):
        """Legacy 'cook' maps to 'Pro'."""
        from app.tier_labels import display_label
        assert display_label("cook") == "Pro"


# ── Checkout URL alias rewriting ──────────────────────────────────────────

class TestCheckoutUrlAliasRewriting:
    """Test that legacy tier URLs /api/checkout/cook etc. are rewritten to canonical."""

    def _build_app(self, db: Session, user: User):
        from fastapi import FastAPI
        from app.checkout_routes import router as checkout_router
        from app.database import get_db
        from app import auth_routes

        app = FastAPI()

        def _override_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[auth_routes.get_current_user_optional] = lambda: user
        app.include_router(checkout_router)
        return app

    def test_checkout_cook_rewrites_to_pro(self, db):
        """POST /api/checkout/cook → rewrites to 'pro' and creates session."""
        from fastapi.testclient import TestClient
        from app import subscription_service as ss

        user = _make_user(db, tier="pro", sub_id="sub_alias_cook")
        user.stripe_customer_id = "cus_alias_cook"
        db.commit()

        app = self._build_app(db, user)
        client = TestClient(app)

        fake_session = {"id": "cs_alias_cook", "url": "https://checkout.stripe.com/cs_alias_cook"}
        fake_customer = {"id": "cus_alias_cook"}

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_t", "pro_plus": "price_pp_t"}), \
             patch("stripe.Customer.create", return_value=fake_customer), \
             patch("stripe.checkout.Session.create", return_value=fake_session), \
             patch("stripe.PromotionCode.list", return_value={"data": []}):
            resp = client.post("/api/checkout/cook")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tier"] == "pro"

    def test_checkout_operator_rewrites_to_pro_plus(self, db):
        """POST /api/checkout/operator → rewrites to 'pro_plus' and creates session."""
        from fastapi.testclient import TestClient
        from app import subscription_service as ss

        user = _make_user(db, tier="pro_plus", sub_id="sub_alias_op")
        user.stripe_customer_id = "cus_alias_op"
        db.commit()

        app = self._build_app(db, user)
        client = TestClient(app)

        fake_session = {"id": "cs_alias_op", "url": "https://checkout.stripe.com/cs_alias_op"}
        fake_customer = {"id": "cus_alias_op"}

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_t", "pro_plus": "price_pp_t"}), \
             patch("stripe.Customer.create", return_value=fake_customer), \
             patch("stripe.checkout.Session.create", return_value=fake_session), \
             patch("stripe.PromotionCode.list", return_value={"data": []}):
            resp = client.post("/api/checkout/operator")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tier"] == "pro_plus"

    def test_checkout_studio_rewrites_to_pro_plus(self, db):
        """POST /api/checkout/studio → rewrites to 'pro_plus'."""
        from fastapi.testclient import TestClient
        from app import subscription_service as ss

        user = _make_user(db, tier="pro_plus", sub_id="sub_alias_studio")
        user.stripe_customer_id = "cus_alias_studio"
        db.commit()

        app = self._build_app(db, user)
        client = TestClient(app)

        fake_session = {"id": "cs_alias_studio", "url": "https://checkout.stripe.com/cs_alias_studio"}
        fake_customer = {"id": "cus_alias_studio"}

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_t", "pro_plus": "price_pp_t"}), \
             patch("stripe.Customer.create", return_value=fake_customer), \
             patch("stripe.checkout.Session.create", return_value=fake_session), \
             patch("stripe.PromotionCode.list", return_value={"data": []}):
            resp = client.post("/api/checkout/studio")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tier"] == "pro_plus"

    def test_checkout_pro_canonical_still_works(self, db):
        """POST /api/checkout/pro works directly (canonical slug)."""
        from fastapi.testclient import TestClient
        from app import subscription_service as ss

        user = _make_user(db, tier="pro", sub_id="sub_canonical_pro")
        user.stripe_customer_id = "cus_canonical_pro"
        db.commit()

        app = self._build_app(db, user)
        client = TestClient(app)

        fake_session = {"id": "cs_pro", "url": "https://checkout.stripe.com/cs_pro"}
        fake_customer = {"id": "cus_canonical_pro"}

        with patch.object(ss, "TIER_PRICE_IDS", {"pro": "price_pro_t", "pro_plus": "price_pp_t"}), \
             patch("stripe.Customer.create", return_value=fake_customer), \
             patch("stripe.checkout.Session.create", return_value=fake_session), \
             patch("stripe.PromotionCode.list", return_value={"data": []}):
            resp = client.post("/api/checkout/pro")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tier"] == "pro"
