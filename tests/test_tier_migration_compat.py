"""Tests for RCP-INCIDENT-2026-05-11 Phase 3: studio→operator migration compat.

Covers:
1. _apply_subscription_state with price.metadata.tier='studio' → user ends up with
   subscription_tier='operator' (shim)
2. downgrade_studio_to_cook still works (delegates to operator function)
3. tier_labels._is_operator_tier('studio') == True (legacy accepted)
4. tier_labels._is_operator_tier('operator') == True
5. tier_labels._is_operator_tier('cook') == False
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


def _make_user(db, tier="operator", status="active", sub_id="sub_test_001") -> User:
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


# ── 1. _apply_subscription_state with tier='studio' → operator shim ──────

class TestApplySubscriptionStateShim:
    def test_studio_tier_in_price_metadata_normalised_to_operator(self, db):
        """price.metadata.tier='studio' → user.subscription_tier='operator' via shim."""
        import importlib
        from app import subscription_service as ss
        ss = importlib.reload(ss)

        user = _make_user(db, tier=None, status=None, sub_id="sub_shim_test")
        user.subscription_tier = None
        db.commit()

        period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        fake_sub = {
            "id": "sub_shim_test",
            "status": "active",
            "current_period_end": period_end,
            "items": {"data": [
                {"price": {"id": "price_studio_legacy", "metadata": {"tier": "studio"}}},
            ]},
            "metadata": {"wiserecipes_user_id": str(user.id)},
        }

        # Patch TIER_PRICE_IDS so price id reverse-lookup doesn't match
        with patch.object(ss, "TIER_PRICE_IDS", {"cook": "price_cook_x", "operator": "price_op_x"}):
            ss._apply_subscription_state(user, fake_sub, db)

        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "operator", (
            f"Expected 'operator', got {fresh.subscription_tier!r} — shim not working"
        )

    def test_studio_slug_in_price_id_reverse_lookup_normalised(self, db):
        """If tier comes from price-id reverse-lookup and the key is 'operator',
        it should remain 'operator'."""
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
                # price id matches 'operator' key in TIER_PRICE_IDS
                {"price": {"id": "price_op_match", "metadata": {}}},
            ]},
            "metadata": {"wiserecipes_user_id": str(user.id)},
        }

        with patch.object(ss, "TIER_PRICE_IDS", {"cook": "price_cook_x", "operator": "price_op_match"}):
            ss._apply_subscription_state(user, fake_sub, db)

        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "operator"


# ── 2. downgrade_studio_to_cook delegates to downgrade_operator_to_cook ──

class TestDowngradeStudioWrapper:
    def test_downgrade_studio_to_cook_delegates_for_studio_user(self, db):
        """downgrade_studio_to_cook() with a 'studio' user → delegates, returns cook."""
        from app import subscription_service as ss

        user = _make_user(db, tier="studio", sub_id="sub_downgrade_studio")
        # 'studio' user still accepted via shim in downgrade_operator_to_cook

        fake_sub = {"id": user.subscription_id, "items": {"data": [{"id": "si_001"}]}}
        fake_modified = {"id": user.subscription_id, "status": "active"}

        with patch("stripe.Subscription.retrieve", return_value=fake_sub), \
             patch("stripe.Subscription.modify", return_value=fake_modified), \
             patch.object(ss, "TIER_PRICE_IDS", {"cook": "price_cook_test", "operator": "price_op_test"}):
            result = ss.downgrade_studio_to_cook(user, db)

        assert result["tier"] == "cook"
        db.expire_all()
        fresh = db.query(User).filter(User.id == user.id).first()
        assert fresh.subscription_tier == "cook"

    def test_downgrade_studio_to_cook_delegates_for_operator_user(self, db):
        """downgrade_studio_to_cook() with an 'operator' user → delegates successfully."""
        from app import subscription_service as ss

        user = _make_user(db, tier="operator", sub_id="sub_downgrade_op")

        fake_sub = {"id": user.subscription_id, "items": {"data": [{"id": "si_002"}]}}
        fake_modified = {"id": user.subscription_id, "status": "active"}

        with patch("stripe.Subscription.retrieve", return_value=fake_sub), \
             patch("stripe.Subscription.modify", return_value=fake_modified), \
             patch.object(ss, "TIER_PRICE_IDS", {"cook": "price_cook_test", "operator": "price_op_test"}):
            result = ss.downgrade_studio_to_cook(user, db)

        assert result["tier"] == "cook"

    def test_downgrade_rejects_cook_user(self, db):
        """downgrade_operator_to_cook raises for a cook user."""
        from app.subscription_service import downgrade_operator_to_cook, SubscriptionError

        user = _make_user(db, tier="cook", sub_id="sub_cook_user")
        with pytest.raises(SubscriptionError, match="not_operator"):
            downgrade_operator_to_cook(user, db)


# ── 3-5. tier_labels helpers ─────────────────────────────────────────────

class TestTierLabelHelpers:
    def test_is_operator_tier_studio(self):
        """Legacy 'studio' slug accepted by _is_operator_tier."""
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier("studio") is True

    def test_is_operator_tier_operator(self):
        """Canonical 'operator' slug accepted."""
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier("operator") is True

    def test_is_operator_tier_cook(self):
        """'cook' is not operator tier."""
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier("cook") is False

    def test_is_operator_tier_none(self):
        """None is not operator tier."""
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier(None) is False

    def test_is_operator_tier_free(self):
        """'free' is not operator tier."""
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier("free") is False

    def test_is_paid_tier_studio_legacy(self):
        """Legacy 'studio' slug accepted as paid tier."""
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("studio") is True

    def test_is_paid_tier_cook(self):
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("cook") is True

    def test_is_paid_tier_operator(self):
        from app.tier_labels import _is_paid_tier
        assert _is_paid_tier("operator") is True

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
