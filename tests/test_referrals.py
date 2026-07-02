"""Tests for referral tracking — WIS-660.

4 tests:
1. Cookie roundtrip — referral cookie is read during OAuth callback
2. Signin persistence — referral row is created linking referrer → referred
3. Invoice attribution — first Stripe invoice accrues referrer share
4. Rate-lock enforcement — first 50 referrers get 50%, subsequent get 30%
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4, UUID
from unittest.mock import patch, MagicMock

from app.models import Base, User, Referral, CreatorPayout
from app.referral import (
    generate_referral_code,
    ensure_referral_code,
    process_referral_cookie,
    REFERRAL_COOKIE_NAME,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_user(db, display_name="Test User", referral_code=None, **kw) -> User:
    u = User(
        id=uuid4(),
        display_name=display_name,
        referral_code=referral_code,
        **kw,
    )
    db.add(u)
    db.flush()
    return u


# ── Test 1: Cookie roundtrip ────────────────────────────────────────────

def test_referral_cookie_sets_and_reads(db_session):
    """Simulate: referrer has a code, new user signs in with ?ref=CODE cookie."""
    ref_code = generate_referral_code()
    referrer = _make_user(db_session, "Referrer", referral_code=ref_code)
    db_session.commit()

    new_user = _make_user(db_session, "NewUser")
    db_session.commit()

    # Process the referral cookie (simulating what auth callback does)
    result = process_referral_cookie(db_session, new_user, ref_code)

    assert result is not None
    assert result.referrer_user_id == referrer.id
    assert result.referred_user_id == new_user.id
    assert result.referral_code == ref_code
    assert result.status == "signed_up"

    # Verify referred_by is set on user
    db_session.refresh(new_user)
    assert new_user.referred_by == referrer.id


def test_referral_cookie_missing_code(db_session):
    """No ref cookie → no referral row."""
    new_user = _make_user(db_session, "Solo")
    db_session.commit()

    result = process_referral_cookie(db_session, new_user, None)
    assert result is None

    result2 = process_referral_cookie(db_session, new_user, "nonexistent")
    assert result2 is None


# ── Test 2: Signin persistence ──────────────────────────────────────────

def test_referral_persists_across_signin(db_session):
    """Referral row survives and links referrer → referred user."""
    ref_code = generate_referral_code()
    referrer = _make_user(db_session, "Referrer", referral_code=ref_code)
    db_session.commit()

    new_user = _make_user(db_session, "Referred")
    db_session.commit()

    referral = process_referral_cookie(db_session, new_user, ref_code)
    assert referral is not None

    # Verify persistence by querying fresh
    from sqlalchemy.orm import Session
    db_referral = db_session.query(Referral).filter(Referral.id == referral.id).first()
    assert db_referral is not None
    assert db_referral.referrer_user_id == referrer.id
    assert db_referral.referred_user_id == new_user.id
    assert db_referral.rate is not None


# ── Test 3: Invoice attribution ─────────────────────────────────────────

def test_invoice_accrues_referrer_share(db_session):
    """First invoice on referred user accrues 50% to referrer's creator_payouts."""
    ref_code = generate_referral_code()
    referrer = _make_user(db_session, "Referrer", referral_code=ref_code)
    db_session.commit()

    new_user = _make_user(
        db_session, "Referred",
        referred_by=referrer.id,
        subscription_id="sub_test_invoice",
        subscription_tier="cook",
        subscription_status="active",
    )
    db_session.commit()

    # Create the referral row (as would happen during signup)
    referral = Referral(
        id=uuid4(),
        referrer_user_id=referrer.id,
        referred_user_id=new_user.id,
        referral_code=ref_code,
        status="signed_up",
        rate=Decimal("0.50"),
    )
    db_session.add(referral)
    db_session.commit()

    # Mock Stripe subscription retrieval
    with patch("app.subscription_service.stripe") as mock_stripe:
        mock_stripe.Subscription.retrieve.return_value = {
            "items": {"data": [{"plan": {"amount": 500}}]}  # 500 cents = $5.00
        }
        
        from app.subscription_service import _accrue_referral_on_first_payment
        _accrue_referral_on_first_payment(new_user, db_session)

    # Verify referral converted
    db_session.refresh(referral)
    assert referral.status == "converted"
    assert referral.reward_cents == 250  # 50% of 500 (cook tier default)

    # Verify payout row created for referrer
    payout = (
        db_session.query(CreatorPayout)
        .filter(CreatorPayout.creator_id == referrer.id)
        .first()
    )
    assert payout is not None
    assert payout.creator_share_cents == 250
    assert payout.status == "pending"


# ── Test 4: Rate-lock enforcement ───────────────────────────────────────

def test_rate_lock_first_50_vs_rest(db_session):
    """First 50 referrers get 50%, subsequent get 30%."""
    ref_code = generate_referral_code()
    referrer = _make_user(db_session, "EarlyReferrer", referral_code=ref_code)
    db_session.commit()

    # First referral → rate should be 0.50 (referrer_rank < 50)
    new_user_1 = _make_user(db_session, "Referred1")
    db_session.commit()
    r1 = process_referral_cookie(db_session, new_user_1, ref_code)
    assert float(r1.rate) == 0.50

    # Simulate 49 more referrals to push rank to 50
    for i in range(49):
        u = _make_user(db_session, f"Filler{i}")
        db_session.commit()
        process_referral_cookie(db_session, u, ref_code)

    # 51st referral → rate should be 0.30
    new_user_51 = _make_user(db_session, "Referred51")
    db_session.commit()
    r51 = process_referral_cookie(db_session, new_user_51, ref_code)
    assert float(r51.rate) == 0.30


def test_ensure_referral_code(db_session):
    """ensure_referral_code generates and persists a code."""
    user = _make_user(db_session, "Codeless")
    db_session.commit()

    code = ensure_referral_code(user, db_session)
    assert code is not None
    assert len(code) == 8

    # Calling again returns same code
    code2 = ensure_referral_code(user, db_session)
    assert code2 == code


def test_no_self_referral(db_session):
    """Users cannot refer themselves."""
    ref_code = generate_referral_code()
    user = _make_user(db_session, "SelfRef", referral_code=ref_code)
    db_session.commit()

    result = process_referral_cookie(db_session, user, ref_code)
    assert result is None
