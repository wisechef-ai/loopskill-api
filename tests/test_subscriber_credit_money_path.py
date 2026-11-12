"""MONEY-PATH TESTS — app/subscriber_credit_service.py (contributor-discount credits).

WHY THIS PATH IS CRITICAL
-------------------------
`subscriber_credit_service.py` mints and redeems REAL discount value: a 50%
coupon on a paying subscriber's next Stripe invoice. Coverage measured
2026-10-28 (t_1b7e61a5): **0.0% — 59/59 statements missed, lines 22-225**,
zero test files, for a second straight month (also 0% at the 2026-09-26
seed). This is the only module in the repo that writes billing-facing rows
AND calls `stripe.Coupon.create`. A silent regression here means:

  * credits granted to non-paying users      → revenue leak,
  * the idempotency/dedup guards weakening   → stacked 50% credits,
  * `apply_credit_to_stripe_coupon` failing  → approved contributors never
    get their promised discount (support load, churn),
  * `expire_stale_credits` never tombstoning → stale credits resurface
    months later on a live invoice.

WHAT IS TESTED (contract pinned, not implementation-mocked)
-----------------------------------------------------------
grant_contributor_credit:
  1. pro user + fresh skill        -> one 50% credit, persisted, type pinned
  2. same (user, skill) again      -> SAME row (idempotent), no duplicate
  3. free tier / unknown user      -> None, no row written
  4. tier normalization + aliases  -> "PRO", "operator", "studio" qualify
  5. unused credit, OTHER skill    -> returns the EXISTING credit, no new row
  6. consumed credit (used_at set) -> a NEW credit MAY be granted
  7. expiry                        -> period_end + 90d when renewal known,
                                       ~now + 180d fallback otherwise
apply_credit_to_stripe_coupon:
  8. happy path                    -> one-time coupon, all create() kwargs
                                       pinned incl. idempotency_key + metadata
  9. missing credit / used credit  -> ValueError (fail closed, no stripe call)
expire_stale_credits:
 10. tombstones ONLY expired+unused, returns count, second run is a no-op
"""

from __future__ import annotations

import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.conftest import make_skill

from app.subscriber_credit_service import (
    apply_credit_to_stripe_coupon,
    expire_stale_credits,
    grant_contributor_credit,
)

from app.models import SubscriberCredit, User


def _naive(dt):
    """Normalize possibly-aware datetimes for cross-dialect comparison.

    SQLite returns naive datetimes; Postgres (DateTime(timezone=True))
    returns aware. Tests compare values, not tz bookkeeping.
    """
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _make_user(db, tier="pro", period_end=None):
    u = User(
        id=uuid.uuid4(),
        display_name="credit-test",
        email=f"{uuid.uuid4()}@test.example",
        subscription_tier=tier,
        subscription_status="active",
        subscription_current_period_end=period_end,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _credits_for(db, user):
    return db.query(SubscriberCredit).filter(SubscriberCredit.user_id == user.id).all()


# ── grant_contributor_credit ────────────────────────────────────────────────


def test_grant_pro_user_gets_one_50pct_credit(db_session):
    user = _make_user(db_session, tier="pro")
    skill = make_skill(db_session, slug="credit-skill-1")

    credit = grant_contributor_credit(db_session, user.id, skill.id)

    assert credit is not None
    assert credit.user_id == user.id
    assert credit.type == "contributor_discount"
    assert credit.amount_pct == 50
    assert credit.granted_for_skill_id == skill.id
    assert credit.used_at is None  # active/available
    assert len(_credits_for(db_session, user)) == 1


def test_grant_is_idempotent_for_same_user_and_skill(db_session):
    user = _make_user(db_session, tier="pro")
    skill = make_skill(db_session, slug="credit-skill-2")

    first = grant_contributor_credit(db_session, user.id, skill.id)
    second = grant_contributor_credit(db_session, user.id, skill.id)

    assert second.id == first.id
    assert len(_credits_for(db_session, user)) == 1  # no duplicate row


def test_grant_free_tier_and_unknown_user_return_none(db_session):
    free_user = _make_user(db_session, tier="free")
    skill = make_skill(db_session, slug="credit-skill-3")

    assert grant_contributor_credit(db_session, free_user.id, skill.id) is None
    assert _credits_for(db_session, free_user) == []  # nothing written

    unknown = uuid.uuid4()  # no such user row
    assert grant_contributor_credit(db_session, unknown, skill.id) is None


def test_grant_tier_is_case_insensitive_and_covers_legacy_aliases(db_session):
    # .lower() normalization + legacy alias tiers (operator/studio) must both
    # qualify — these are 30-day aliases of pro per the tier map.
    for tier in ("PRO", "Operator", "studio", "pro_plus"):
        user = _make_user(db_session, tier=tier)
        skill = make_skill(db_session, slug=f"alias-{tier.lower()}")
        assert grant_contributor_credit(db_session, user.id, skill.id) is not None, tier


def test_grant_dedupes_unused_credit_across_skills(db_session):
    user = _make_user(db_session, tier="pro")
    skill_a = make_skill(db_session, slug="dedup-a")
    skill_b = make_skill(db_session, slug="dedup-b")

    first = grant_contributor_credit(db_session, user.id, skill_a.id)
    returned = grant_contributor_credit(db_session, user.id, skill_b.id)

    # Must return the EXISTING unused credit (consume-first rule), not a new one.
    assert returned.id == first.id
    assert returned.granted_for_skill_id == skill_a.id
    assert len(_credits_for(db_session, user)) == 1


def test_grant_after_consumed_credit_allows_a_new_grant(db_session):
    user = _make_user(db_session, tier="pro")
    skill_old = make_skill(db_session, slug="consumed-old")
    skill_new = make_skill(db_session, slug="consumed-new")

    first = grant_contributor_credit(db_session, user.id, skill_old.id)
    first.used_at = datetime.now(UTC)  # consumed (or tombstoned by the cron)
    db_session.commit()

    second = grant_contributor_credit(db_session, user.id, skill_new.id)

    assert second is not None and second.id != first.id
    assert len(_credits_for(db_session, user)) == 2


def test_grant_expiry_is_period_end_plus_90_days(db_session):
    period_end = datetime(2030, 3, 1, tzinfo=UTC)
    user = _make_user(db_session, tier="pro", period_end=period_end)
    skill = make_skill(db_session, slug="expiry-90d")

    credit = grant_contributor_credit(db_session, user.id, skill.id)

    assert _naive(credit.expires_at) == _naive(period_end + timedelta(days=90))


def test_grant_expiry_falls_back_to_180_days_without_renewal_date(db_session):
    user = _make_user(db_session, tier="pro", period_end=None)
    skill = make_skill(db_session, slug="expiry-180d")

    before = datetime.now(UTC)
    credit = grant_contributor_credit(db_session, user.id, skill.id)
    after = datetime.now(UTC)

    expected_min = _naive(before + timedelta(days=180))
    expected_max = _naive(after + timedelta(days=180))
    assert expected_min <= _naive(credit.expires_at) <= expected_max


# ── apply_credit_to_stripe_coupon ───────────────────────────────────────────


def test_apply_coupon_creates_one_time_stripe_coupon(db_session, monkeypatch):
    from app.config import settings

    user = _make_user(db_session, tier="pro")
    skill = make_skill(db_session, slug="coupon-skill")
    credit = grant_contributor_credit(db_session, user.id, skill.id)

    create = MagicMock(return_value=SimpleNamespace(id="coupon_test_123"))
    fake_stripe = types.ModuleType("stripe")
    fake_stripe.Coupon = SimpleNamespace(create=create)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)

    coupon_id = apply_credit_to_stripe_coupon(db_session, credit.id)

    assert coupon_id == "coupon_test_123"
    assert fake_stripe.api_key == settings.STRIPE_SECRET_KEY  # key threaded, not hardcoded

    (args, kwargs) = create.call_args
    assert args == ()
    assert kwargs["percent_off"] == 50
    assert kwargs["duration"] == "once"          # single invoice only
    assert kwargs["max_redemptions"] == 1        # one-time use
    assert kwargs["idempotency_key"] == f"coupon_{credit.id}"  # replay-safe
    assert kwargs["metadata"] == {
        "subscriber_credit_id": str(credit.id),
        "user_id": str(user.id),
    }


def test_apply_coupon_fails_closed_for_missing_and_used_credits(db_session, monkeypatch):
    create = MagicMock()
    fake_stripe = types.ModuleType("stripe")
    fake_stripe.Coupon = SimpleNamespace(create=create)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)

    # Missing credit: ValueError, and Stripe must never be called.
    with pytest.raises(ValueError, match="not found"):
        apply_credit_to_stripe_coupon(db_session, uuid.uuid4())
    create.assert_not_called()

    # Already-used credit: ValueError, no Stripe call, no coupon leaked.
    user = _make_user(db_session, tier="pro")
    skill = make_skill(db_session, slug="coupon-used")
    credit = grant_contributor_credit(db_session, user.id, skill.id)
    credit.used_at = datetime.now(UTC)
    db_session.commit()

    with pytest.raises(ValueError, match="already used"):
        apply_credit_to_stripe_coupon(db_session, credit.id)
    create.assert_not_called()


# ── expire_stale_credits ────────────────────────────────────────────────────


def test_expire_stale_credits_tombstones_only_expired_unused(db_session):
    user = _make_user(db_session, tier="pro")
    # Seed tz-aware datetimes: the service filters with an AWARE now(UTC), and
    # SQLAlchemy's synchronize_session evaluates the criteria in Python against
    # these in-memory rows — naive-vs-aware raises TypeError. Production
    # (Postgres, DateTime(timezone=True)) is aware end-to-end, so aware seeds
    # mirror the real dialect.
    stale = SubscriberCredit(
        user_id=user.id,
        type="contributor_discount",
        amount_pct=50,
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),  # long expired, unused
    )
    already_used = SubscriberCredit(
        user_id=user.id,
        type="contributor_discount",
        amount_pct=50,
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        used_at=datetime(2020, 2, 1, tzinfo=UTC),  # expired but already consumed
    )
    still_valid = SubscriberCredit(
        user_id=user.id,
        type="contributor_discount",
        amount_pct=50,
        expires_at=datetime(2050, 1, 1, tzinfo=UTC),  # not yet expired
    )
    db_session.add_all([stale, already_used, still_valid])
    db_session.commit()

    expired_count = expire_stale_credits(db_session)

    assert expired_count == 1
    db_session.refresh(stale)
    db_session.refresh(already_used)
    db_session.refresh(still_valid)
    assert stale.used_at is not None           # tombstoned by the cron
    assert _naive(already_used.used_at) == datetime(2020, 2, 1)  # untouched
    assert still_valid.used_at is None         # future credit survives

    # Idempotent: re-running the nightly cron must not re-count anything.
    # expunge_all() first: refresh() above reloaded the rows as NAIVE
    # datetimes (sqlite dialect), and the ORM evaluates the update criteria
    # in Python against loaded rows — naive-vs-aware raises TypeError. On
    # Postgres (production) refresh keeps them aware and this is a non-issue.
    db_session.expunge_all()
    assert expire_stale_credits(db_session) == 0
