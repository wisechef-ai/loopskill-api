"""Tests for referral/affiliate tracking (WIS-660).

Covers:
1. Cookie roundtrip: ?ref=ABC → cookie set → signin → referrals row created
2. Signin persistence: referral row idempotent on repeated cookie
3. Invoice attribution: first payment accrues referrer share
4. Rate-lock enforcement: first 50 referrers get 50%, 51st gets 30%
"""

import pytest
from unittest.mock import patch, MagicMock
from uuid import uuid4, UUID
from datetime import datetime, timezone
from decimal import Decimal

from app.models import User, Referral, CreatorPayout
from app.referral import (
    generate_referral_code,
    ensure_referral_code,
    process_referral_cookie,
    REFERRAL_COOKIE_NAME,
)


def _make_user(db, email="ref@example.com", **kwargs):
    """Create and flush a User for referral tests."""
    defaults = dict(
        display_name="Ref TestUser",
        github_id=hash(email) % 2**31,
    )
    defaults.update(kwargs)
    user = User(id=uuid4(), email=email, **defaults)
    db.add(user)
    db.flush()
    return user


class TestCookieRoundtrip:
    """1. Setting ref cookie and creating referral row on signin."""

    def test_process_referral_cookie_creates_row(self, db_session):
        """Valid ref code creates Referral row linking referrer → new user."""
        referrer = _make_user(db_session, email="referrer@test.com", referral_code="ABC12345")
        new_user = _make_user(db_session, email="newbie@test.com")

        referral = process_referral_cookie(db_session, new_user, "ABC12345")

        assert referral is not None
        assert referral.referrer_user_id == referrer.id
        assert referral.referred_user_id == new_user.id
        assert referral.referral_code == "ABC12345"
        assert referral.status == "signed_up"
        assert new_user.referred_by == referrer.id

    def test_process_referral_cookie_missing_code_returns_none(self, db_session):
        """None ref code returns None without error."""
        user = _make_user(db_session, email="no_ref@test.com")
        result = process_referral_cookie(db_session, user, None)
        assert result is None

    def test_process_referral_cookie_invalid_code_returns_none(self, db_session):
        """Unknown ref code returns None, logs warning."""
        user = _make_user(db_session, email="bad_code@test.com")
        result = process_referral_cookie(db_session, user, "NOTFOUND")
        assert result is None


class TestSigninPersistence:
    """2. Referral row is idempotent on repeated cookie."""

    def test_idempotent_on_repeated_cookie(self, db_session):
        """Calling process_referral_cookie twice returns same row."""
        referrer = _make_user(db_session, email="ref_idem@test.com", referral_code="IDEM1234")
        new_user = _make_user(db_session, email="idem_new@test.com")

        first = process_referral_cookie(db_session, new_user, "IDEM1234")
        second = process_referral_cookie(db_session, new_user, "IDEM1234")

        assert first.id == second.id
        # Only one referral row
        count = db_session.query(Referral).filter(
            Referral.referrer_user_id == referrer.id,
            Referral.referred_user_id == new_user.id,
        ).count()
        assert count == 1

    def test_no_self_referral(self, db_session):
        """User cannot refer themselves."""
        user = _make_user(db_session, email="self@test.com", referral_code="SELF1234")
        result = process_referral_cookie(db_session, user, "SELF1234")
        assert result is None


class TestInvoiceAttribution:
    """3. First payment accrues referrer share via _accrue_referral_on_first_payment."""

    @patch("app.subscription_service.stripe")
    def test_first_payment_accrues_referral_payout(self, mock_stripe, db_session):
        """First payment creates CreatorPayout row for referrer."""
        from app.subscription_service import _accrue_referral_on_first_payment

        referrer = _make_user(db_session, email="ref_pay@test.com", referral_code="PAYM1234")
        new_user = _make_user(
            db_session, email="pay_new@test.com",
            referred_by=referrer.id,
            subscription_id="sub_test_123",
            subscription_tier="cook",
            subscription_status="active",
        )
        # Create the referral row
        referral = Referral(
            id=uuid4(),
            referrer_user_id=referrer.id,
            referred_user_id=new_user.id,
            referral_code="PAYM1234",
            referred_email="pay_new@test.com",
            status="signed_up",
            rate=Decimal("0.50"),
        )
        db_session.add(referral)
        db_session.flush()

        # Mock Stripe subscription retrieval
        mock_stripe.Subscription.retrieve.return_value = {
            "items": {"data": [{"plan": {"amount": 500}}]}
        }

        _accrue_referral_on_first_payment(new_user, db_session)

        # Verify referral updated
        db_session.refresh(referral)
        assert referral.status == "converted"
        assert referral.reward_cents == 250  # 500 * 0.50
        assert referral.converted_at is not None

        # Verify payout created
        payout = db_session.query(CreatorPayout).filter(
            CreatorPayout.creator_id == referrer.id
        ).first()
        assert payout is not None
        assert payout.creator_share_cents == 250
        assert payout.status == "pending"

    def test_no_payout_if_not_referred(self, db_session):
        """User without referred_by skips accrual silently."""
        from app.subscription_service import _accrue_referral_on_first_payment

        user = _make_user(db_session, email="no_ref_pay@test.com", subscription_tier="cook")
        # Should not raise
        _accrue_referral_on_first_payment(user, db_session)

        payouts = db_session.query(CreatorPayout).all()
        assert len(payouts) == 0


class TestRateLockEnforcement:
    """4. First 50 referrers locked at 50%, 51st gets 30%."""

    def test_first_referral_gets_50_percent(self, db_session):
        """Referrer with 0 prior referrals gets 50% rate."""
        referrer = _make_user(db_session, email="r50@test.com", referral_code="RATE50AB")
        new_user = _make_user(db_session, email="n50@test.com")

        referral = process_referral_cookie(db_session, new_user, "RATE50AB")
        assert referral.rate == Decimal("0.50")

    def test_51st_referral_gets_30_percent(self, db_session):
        """Referrer with 50+ prior referrals gets 30% rate.

        We simulate by creating 50 existing referral rows for the referrer.
        """
        referrer = _make_user(db_session, email="r51@test.com", referral_code="RATE51AB")

        # Create 50 existing referrals
        for i in range(50):
            dummy = _make_user(db_session, email=f"dummy{i}@test.com")
            existing_ref = Referral(
                id=uuid4(),
                referrer_user_id=referrer.id,
                referred_user_id=dummy.id,
                referral_code="RATE51AB",
                status="signed_up",
                rate=Decimal("0.50"),
            )
            db_session.add(existing_ref)
        db_session.flush()

        # 51st referral
        new_user = _make_user(db_session, email="n51@test.com")
        referral = process_referral_cookie(db_session, new_user, "RATE51AB")
        assert referral.rate == Decimal("0.30")


class TestReferralCodeGeneration:
    """Bonus: referral code generation."""

    def test_generate_referral_code_length(self):
        code = generate_referral_code()
        assert len(code) == 8

    def test_generate_referral_code_base62(self):
        import string
        valid = set(string.ascii_letters + string.digits)
        code = generate_referral_code()
        assert all(c in valid for c in code)

    def test_ensure_referral_code_creates_one(self, db_session):
        user = _make_user(db_session, email="ensure@test.com")
        code = ensure_referral_code(user, db_session)
        assert code is not None
        assert len(code) == 8
        db_session.refresh(user)
        assert user.referral_code == code
