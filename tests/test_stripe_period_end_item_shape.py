"""Regression test for Stripe API 2025-03+ ``current_period_end`` relocation.

On Stripe API versions >= 2025-03-31.basil, ``current_period_end`` (and
``current_period_start``) moved from the top-level Subscription object to
each entry in ``items.data[]``. LIVE PROOF (2026-09-06, prod, 3 real subs):
Stripe returned ``status=active`` with the top-level key ABSENT and
``items.data[0].current_period_end`` populated; the DB's
``subscription_current_period_end`` column froze at signup because
``_apply_subscription_state`` only ever read the (now-empty) top-level key.

This module pins two fixture shapes:
  * ``_MODERN_SUB`` — the REAL shape returned by the pinned API version
    (``2026-01-28.clover``, itself >= 2025-03-31.basil): no top-level
    ``current_period_end``, only per-item.
  * the old-shape dict used throughout ``tests/test_subscription.py`` (top-level
    key present, no per-item value) — kept passing so pre-migration fixtures
    and any Stripe object cached/logged before this fix still resolve.

``_subscription_period_end`` must resolve BOTH shapes; when both are present
it takes the max across ``items.data[]`` (a subscription can carry multiple
line items with different renewal boundaries after proration/upgrades).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, User
from app.subscription_service import _apply_subscription_state, _subscription_period_end


# ── DB fixtures (isolated in-memory SQLite, same pattern as test_subscription.py) ──


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


@pytest.fixture
def test_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        github_id=999_500 + int(uuid.uuid4().int) % 1_000_000,
        email=f"period-end-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="Period-End Test User",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


_NOW = int(datetime.now(tz=UTC).timestamp())
_ITEM_PERIOD_END = _NOW + 5 * 24 * 3600  # +5 days
_ITEM_PERIOD_END_LATER = _NOW + 27 * 24 * 3600  # +27 days, later item

# The REAL shape returned by Stripe API >= 2025-03-31.basil (matches the
# pinned 2026-01-28.clover live-proof payload): top-level current_period_end
# is ABSENT; each item carries its own.
_MODERN_SUB = {
    "id": "sub_modern_test",
    "status": "active",
    "items": {
        "data": [
            {
                "price": {"id": "price_test_pro", "metadata": {"tier": "pro"}},
                "current_period_end": _ITEM_PERIOD_END,
                "current_period_start": _NOW,
            }
        ]
    },
}

# Old (pre-2025-03) shape: top-level key present, still the shape used by
# every existing fixture in tests/test_subscription.py — must keep resolving.
_LEGACY_SUB = {
    "id": "sub_legacy_test",
    "status": "active",
    "current_period_end": _ITEM_PERIOD_END,
    "items": {
        "data": [
            {"price": {"id": "price_test_pro", "metadata": {"tier": "pro"}}},
        ]
    },
}

# Multi-item modern subscription — max() across items, not items[0].
_MULTI_ITEM_SUB = {
    "id": "sub_multi_test",
    "status": "active",
    "items": {
        "data": [
            {
                "price": {"id": "price_test_pro", "metadata": {"tier": "pro"}},
                "current_period_end": _ITEM_PERIOD_END,
            },
            {
                "price": {"id": "price_test_addon", "metadata": {}},
                "current_period_end": _ITEM_PERIOD_END_LATER,
            },
        ]
    },
}

# Neither top-level nor item-level present at all (defensive: malformed/empty).
_EMPTY_SUB = {
    "id": "sub_empty_test",
    "status": "active",
    "items": {"data": [{"price": {"id": "price_test_pro"}}]},
}


def test_subscription_period_end_resolves_modern_item_shape():
    """THE bug: modern Stripe payloads carry period_end only on items[]."""
    assert _subscription_period_end(_MODERN_SUB) == _ITEM_PERIOD_END


def test_subscription_period_end_resolves_legacy_top_level_shape():
    """Old fixtures / pre-2025-03 cached objects must keep resolving."""
    assert _subscription_period_end(_LEGACY_SUB) == _ITEM_PERIOD_END


def test_subscription_period_end_takes_max_across_items():
    """Multiple line items: the LATEST renewal boundary wins, not items[0]."""
    assert _subscription_period_end(_MULTI_ITEM_SUB) == _ITEM_PERIOD_END_LATER


def test_subscription_period_end_none_when_absent_everywhere():
    assert _subscription_period_end(_EMPTY_SUB) is None


def test_apply_subscription_state_sets_period_end_from_modern_shape(test_user, db):
    """End-to-end: _apply_subscription_state must not freeze period_end.

    This is the exact regression: on origin/main (pre-fix) this assertion
    fails because ``sub.get('current_period_end')`` is None for the modern
    shape, so ``user.subscription_current_period_end`` is left untouched
    while status/tier/event watermark keep advancing.
    """
    test_user.subscription_current_period_end = None
    _apply_subscription_state(test_user, _MODERN_SUB, db, event_ts=None)
    db.refresh(test_user)

    assert test_user.subscription_current_period_end is not None
    assert test_user.subscription_current_period_end.replace(tzinfo=UTC) == datetime.fromtimestamp(
        _ITEM_PERIOD_END, tz=UTC
    )
    assert test_user.subscription_status == "active"


def test_apply_subscription_state_still_handles_legacy_shape(test_user, db):
    """Non-regression: old top-level-only fixtures keep working."""
    test_user.subscription_current_period_end = None
    _apply_subscription_state(test_user, _LEGACY_SUB, db, event_ts=None)
    db.refresh(test_user)

    assert test_user.subscription_current_period_end.replace(tzinfo=UTC) == datetime.fromtimestamp(
        _ITEM_PERIOD_END, tz=UTC
    )


def test_apply_subscription_state_advances_on_resync_after_freeze(test_user, db):
    """Simulates the exact drift: a stale DB row re-synced from a live read.

    DB starts frozen at a stale value (as in the live incident); re-applying
    the modern subscription object must move it forward to the item-level
    value, proving the resync path (used by the resync script) actually fixes
    drifted rows rather than being a no-op.
    """
    stale = datetime.now(tz=UTC) - timedelta(days=60)
    test_user.subscription_current_period_end = stale
    test_user.subscription_status = "active"

    _apply_subscription_state(test_user, _MODERN_SUB, db, event_ts=None)
    db.refresh(test_user)

    assert test_user.subscription_current_period_end.replace(tzinfo=UTC) != stale
    assert test_user.subscription_current_period_end.replace(tzinfo=UTC) == datetime.fromtimestamp(
        _ITEM_PERIOD_END, tz=UTC
    )


# ── stripe-python StripeObject is NOT a mapping (dict(sub) raises) ──────────────
# Both the resync script and handle_checkout_completed feed a LIVE
# ``stripe.Subscription.retrieve`` result into ``_apply_subscription_state``.
# stripe-python >= 12 raises ``TypeError: Subscription is not iterable or a
# mapping`` on ``dict(sub)`` — proven on prod 2026-09-06 when the resync
# dry-run crashed on the first real subscription. Webhook events are plain
# dicts and must keep working unchanged.


def _live_subscription_object():
    import stripe

    return stripe.Subscription.construct_from(_MODERN_SUB, "sk_test_placeholder")


def test_stripe_object_is_not_a_mapping_in_pinned_sdk():
    """Guards the premise: if the SDK ever makes StripeObject iterable again,
    this test tells us the shim can be deleted."""
    with pytest.raises(TypeError):
        dict(_live_subscription_object())


def test_stripe_to_dict_accepts_live_object_and_plain_dict():
    from app.subscription_service import _stripe_to_dict

    live = _stripe_to_dict(_live_subscription_object())
    assert isinstance(live, dict)
    assert (
        live["items"]["data"][0]["current_period_end"]
        == _MODERN_SUB["items"]["data"][0]["current_period_end"]
    )
    plain = {"id": "sub_plain", "status": "active"}
    assert _stripe_to_dict(plain) is plain


def test_apply_subscription_state_from_live_stripe_object(test_user, db):
    from app.subscription_service import _apply_subscription_state, _stripe_to_dict

    test_user.subscription_current_period_end = None
    _apply_subscription_state(test_user, _stripe_to_dict(_live_subscription_object()), db, event_ts=None)
    db.refresh(test_user)
    assert test_user.subscription_current_period_end is not None
    assert test_user.subscription_current_period_end.replace(tzinfo=UTC) == datetime.fromtimestamp(
        _ITEM_PERIOD_END, tz=UTC
    )
