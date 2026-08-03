"""Tests for scripts/migrate_pro_plus_to_pro.py (autopilot_0308 M2 / D-010).

D-010: the 5 live pro_plus users migrate to pro — script only, never
auto-run. Seeds a DB with 5 synthetic pro_plus users (plus control pro/free
users that must NOT be touched) and exercises:

  - dry-run is the default and writes nothing
  - --execute requires a typed confirmation matching the live count
  - a wrong confirmation aborts with zero writes
  - a successful execute migrates every affected user via the same
    Stripe call sequence as the existing self-serve downgrade endpoint
  - the pro_plus Stripe PRICE object is never touched (only Subscription.modify)
  - idempotency: already-migrated users are skipped, not re-migrated
  - partial failure: one user's Stripe call failing doesn't block the rest,
    and a re-run picks up exactly the still-pending user
  - fails closed: if the affected set changes between the printed plan and
    the confirmation, the whole run aborts with zero writes
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.config import settings
from app.models import User


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def configured_prices(monkeypatch):
    from app import subscription_service as ss

    monkeypatch.setattr(settings, "STRIPE_PRICE_PRO", "price_test_pro")
    monkeypatch.setattr(settings, "STRIPE_PRICE_PRO_PLUS", "price_test_proplus")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "***")
    monkeypatch.setattr(
        ss,
        "TIER_PRICE_IDS",
        {"pro": "price_test_pro", "pro_plus": "price_test_proplus"},
    )
    monkeypatch.setattr(ss, "TIER_USD_PRICE", {"pro": 9.95, "pro_plus": 100.0})
    yield


def make_pro_plus_user(db, n: int) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"proplus-migrate-{n}@example.com",
        display_name=f"Pro Plus User {n}",
        stripe_customer_id=f"cus_test_migrate_{n}",
        subscription_id=f"sub_test_migrate_{n}",
        subscription_status="active",
        subscription_tier="pro_plus",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def five_pro_plus_users(db_session) -> list[User]:
    """Five synthetic pro_plus users, per the DoD's seeded-DB requirement."""
    return [make_pro_plus_user(db_session, n) for n in range(1, 6)]


@pytest.fixture
def control_pro_user(db_session) -> User:
    """Must never be touched by the migration."""
    user = User(
        id=uuid.uuid4(),
        email="control-pro@example.com",
        display_name="Control Pro User",
        stripe_customer_id="cus_control_pro",
        subscription_id="sub_control_pro",
        subscription_status="active",
        subscription_tier="pro",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def control_free_user(db_session) -> User:
    """Must never be touched by the migration."""
    user = User(
        id=uuid.uuid4(),
        email="control-free@example.com",
        display_name="Control Free User",
        subscription_tier=None,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _fake_sub(sub_id: str, item_id: str = "si_test") -> dict:
    return {"id": sub_id, "items": {"data": [{"id": item_id}]}}


def _fake_modified(sub_id: str, item_id: str = "si_test") -> dict:
    return {
        "id": sub_id,
        "status": "active",
        "items": {"data": [{"id": item_id, "price": {"id": "price_test_pro", "metadata": {"tier": "pro"}}}]},
    }


# ── Plan (dry-run) ─────────────────────────────────────────────────────


def test_build_plan_lists_all_five_synthetic_users(configured_prices, db_session, five_pro_plus_users):
    from scripts.migrate_pro_plus_to_pro import build_plan

    plan = build_plan(db_session)
    assert len(plan) == 5
    plan_ids = {p["user_id"] for p in plan}
    assert plan_ids == {u.id for u in five_pro_plus_users}
    for p in plan:
        assert p["current_tier"] == "pro_plus"
        assert p["target_tier"] == "pro"
        assert p["current_price_usd"] == 100.0
        assert p["target_price_usd"] == 9.95
        assert p["stripe_subscription_id"] is not None
        assert p["email"] is not None


def test_build_plan_excludes_non_pro_plus_users(
    configured_prices, db_session, five_pro_plus_users, control_pro_user, control_free_user
):
    from scripts.migrate_pro_plus_to_pro import build_plan

    plan = build_plan(db_session)
    plan_ids = {p["user_id"] for p in plan}
    assert control_pro_user.id not in plan_ids
    assert control_free_user.id not in plan_ids


def test_dry_run_output_contains_audit_fields(configured_prices, db_session, five_pro_plus_users, capsys):
    """DoD: dry-run output must show, per user, id/email/tier/stripe sub id/prices."""
    from scripts.migrate_pro_plus_to_pro import main

    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    for u in five_pro_plus_users:
        assert str(u.id) in out
        assert u.email in out
        assert u.subscription_id in out
    assert "100.00" in out
    assert "9.95" in out


# ── Dry-run writes nothing ─────────────────────────────────────────────


def test_dry_run_default_writes_nothing(configured_prices, db_session, five_pro_plus_users):
    from scripts.migrate_pro_plus_to_pro import main

    with patch("stripe.Subscription.retrieve") as ret_mock, patch("stripe.Subscription.modify") as mod_mock:
        rc = main([])

    assert rc == 0
    ret_mock.assert_not_called()
    mod_mock.assert_not_called()
    for u in five_pro_plus_users:
        db_session.refresh(u)
        assert u.subscription_tier == "pro_plus"


def test_execute_flag_alone_is_not_enough_without_confirmation(
    configured_prices, db_session, five_pro_plus_users
):
    """--execute with a WRONG typed confirmation must abort with zero writes."""
    from scripts.migrate_pro_plus_to_pro import main

    with patch("stripe.Subscription.retrieve") as ret_mock, patch("stripe.Subscription.modify") as mod_mock:
        rc = main(["--execute"], confirm_reader=lambda _prompt: "yes")

    assert rc != 0
    ret_mock.assert_not_called()
    mod_mock.assert_not_called()
    for u in five_pro_plus_users:
        db_session.refresh(u)
        assert u.subscription_tier == "pro_plus"


# ── Execute with correct confirmation ───────────────────────────────────


def test_execute_with_correct_confirmation_migrates_all(
    configured_prices, db_session, five_pro_plus_users, control_pro_user, control_free_user
):
    from scripts.migrate_pro_plus_to_pro import main

    def fake_retrieve(sub_id, **_kw):
        return _fake_sub(sub_id)

    def fake_modify(sub_id, **_kw):
        return _fake_modified(sub_id)

    with patch("stripe.Subscription.retrieve", side_effect=fake_retrieve) as ret_mock, patch(
        "stripe.Subscription.modify", side_effect=fake_modify
    ) as mod_mock:
        rc = main(["--execute"], confirm_reader=lambda _prompt: "MIGRATE 5 USERS")

    assert rc == 0
    assert ret_mock.call_count == 5
    assert mod_mock.call_count == 5

    for u in five_pro_plus_users:
        db_session.refresh(u)
        assert u.subscription_tier == "pro"

    # Controls untouched.
    db_session.refresh(control_pro_user)
    db_session.refresh(control_free_user)
    assert control_pro_user.subscription_tier == "pro"
    assert control_free_user.subscription_tier is None

    # Every Subscription.modify call moved the item to the pro price with
    # proration — never touched a Price object.
    for call in mod_mock.call_args_list:
        assert call.kwargs.get("proration_behavior") == "create_prorations"
        items = call.kwargs.get("items") or []
        assert items and items[0]["price"] == "price_test_pro"


def test_execute_never_calls_price_deactivation(configured_prices, db_session, five_pro_plus_users):
    """Premortem #1: the pro_plus Stripe PRICE must never be archived/deactivated."""
    from scripts.migrate_pro_plus_to_pro import main

    with patch("stripe.Subscription.retrieve", side_effect=lambda sid, **_kw: _fake_sub(sid)), patch(
        "stripe.Subscription.modify", side_effect=lambda sid, **_kw: _fake_modified(sid)
    ), patch("stripe.Price.modify") as price_mod_mock:
        main(["--execute"], confirm_reader=lambda _prompt: "MIGRATE 5 USERS")

    price_mod_mock.assert_not_called()


# ── Idempotency ──────────────────────────────────────────────────────────


def test_rerun_after_full_migration_is_a_noop(configured_prices, db_session, five_pro_plus_users):
    from scripts.migrate_pro_plus_to_pro import main

    with patch("stripe.Subscription.retrieve", side_effect=lambda sid, **_kw: _fake_sub(sid)), patch(
        "stripe.Subscription.modify", side_effect=lambda sid, **_kw: _fake_modified(sid)
    ):
        main(["--execute"], confirm_reader=lambda _prompt: "MIGRATE 5 USERS")

    def _boom(_prompt):
        raise AssertionError("confirmation should never be requested when nothing is pending")

    with patch("stripe.Subscription.retrieve") as ret_mock, patch("stripe.Subscription.modify") as mod_mock:
        rc = main(["--execute"], confirm_reader=_boom)

    assert rc == 0
    ret_mock.assert_not_called()
    mod_mock.assert_not_called()


def test_partial_failure_then_rerun_completes_only_the_pending_user(
    configured_prices, db_session, five_pro_plus_users
):
    """One user's Stripe call fails; the rest still migrate. A second
    --execute run migrates only the failed one — never double-charges or
    re-migrates the four that already succeeded."""
    from scripts.migrate_pro_plus_to_pro import main

    failing_user = five_pro_plus_users[2]

    def fake_retrieve(sub_id, **_kw):
        return _fake_sub(sub_id)

    def fake_modify_first_pass(sub_id, **_kw):
        if sub_id == failing_user.subscription_id:
            raise Exception("stripe_down")
        return _fake_modified(sub_id)

    with patch("stripe.Subscription.retrieve", side_effect=fake_retrieve), patch(
        "stripe.Subscription.modify", side_effect=fake_modify_first_pass
    ):
        rc1 = main(["--execute"], confirm_reader=lambda _prompt: "MIGRATE 5 USERS")
    assert rc1 != 0  # one failure surfaces as a non-zero exit

    for u in five_pro_plus_users:
        db_session.refresh(u)
    still_pending = [u for u in five_pro_plus_users if u.subscription_tier == "pro_plus"]
    migrated_already = [u for u in five_pro_plus_users if u.subscription_tier == "pro"]
    assert [u.id for u in still_pending] == [failing_user.id]
    assert len(migrated_already) == 4

    with patch("stripe.Subscription.retrieve", side_effect=fake_retrieve) as ret_mock, patch(
        "stripe.Subscription.modify", side_effect=lambda sid, **_kw: _fake_modified(sid)
    ) as mod_mock:
        rc2 = main(["--execute"], confirm_reader=lambda _prompt: "MIGRATE 1 USERS")

    assert rc2 == 0
    assert ret_mock.call_count == 1
    assert mod_mock.call_count == 1
    db_session.refresh(failing_user)
    assert failing_user.subscription_tier == "pro"


# ── Fails closed on count mismatch ──────────────────────────────────────


def test_fails_closed_when_affected_set_changes_after_confirmation(
    configured_prices, db_session, five_pro_plus_users
):
    """If a new pro_plus row appears between the printed plan and the typed
    confirmation, the whole run must abort with zero writes rather than act
    on a stale plan."""
    from scripts.migrate_pro_plus_to_pro import main

    def confirm_and_race(_prompt):
        # Simulates a concurrent signup landing while the operator is typing.
        make_pro_plus_user(db_session, 999)
        return "MIGRATE 5 USERS"

    with patch("stripe.Subscription.retrieve") as ret_mock, patch("stripe.Subscription.modify") as mod_mock:
        rc = main(["--execute"], confirm_reader=confirm_and_race)

    assert rc != 0
    ret_mock.assert_not_called()
    mod_mock.assert_not_called()
    for u in five_pro_plus_users:
        db_session.refresh(u)
        assert u.subscription_tier == "pro_plus"


# ── Confirmation phrase helper ───────────────────────────────────────────


def test_confirmation_phrase_format():
    from scripts.migrate_pro_plus_to_pro import confirmation_phrase

    assert confirmation_phrase(5) == "MIGRATE 5 USERS"
    assert confirmation_phrase(1) == "MIGRATE 1 USERS"
