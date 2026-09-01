"""Tests for the $49 one-time Founding Member SKU (capped 100 seats).

feat/founding — stripe-one-time-sku-on-subscription-rail skill pattern.

Covers:
1. Webhook idempotency: a Stripe replay of the same event never double-grants.
2. Grant idempotency: a second grant call on an already-seated user is a no-op replay.
3. Cap enforcement under real concurrency (20 threads, real commits, separate connections).
4. Grant row shape matches scripts/grant_comp_tier.py's comp-grant shape exactly.
5. Route shadowing: POST /api/checkout/founding never reaches the {tier} handler.
6. The pre-existing "skip non-subscription session" webhook path is unbroken by
   the new mode=payment routing (regression pin).
7. Public GET /api/founding/remaining: honest shape, decrements as seats are granted.
8. SSOT guard: no bare 49/100 literal outside config/tiers.yaml.
9. Lost-race auto-refund is attempted (best-effort, idempotency-keyed).
"""

from __future__ import annotations

import tokenize
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import User


def _make_user(db: Session, email: str | None = None, **kwargs) -> User:
    email = email or f"founding-{uuid4()}@example.com"
    defaults = dict(display_name="Founding TestUser", github_id=None)
    defaults.update(kwargs)
    user = User(id=uuid4(), email=email, **defaults)
    db.add(user)
    db.flush()
    return user


def _fake_founding_checkout_session(session_id="cs_founding_1", user_id=None, pi="pi_founding_1"):
    return {
        "id": session_id,
        "mode": "payment",
        "payment_status": "paid",
        "customer": "cus_founding_1",
        "payment_intent": pi,
        "metadata": {"kind": "founding", "loopskill_user_id": str(user_id) if user_id else ""},
    }


def _fake_event(event_type, event_id="evt_founding_001", **session_data):
    return {
        "id": event_id,
        "type": event_type,
        "livemode": False,
        "created": 1_800_000_000,
        "data": {"object": session_data},
    }


# ── 1 & 6: webhook routing + idempotency ────────────────────────────────


class TestWebhookFoundingRouting:
    @patch("app.subscription_service.stripe")
    def test_webhook_grants_founding_membership(self, mock_stripe, db_session):
        from app.subscription_service import handle_checkout_completed

        user = _make_user(db_session)
        session_data = _fake_founding_checkout_session(user_id=user.id)
        event = _fake_event("checkout.session.completed", **session_data)

        result = handle_checkout_completed(event, db_session)
        assert result["processed"] == "checkout.session.completed"
        assert result["granted"] is True
        db_session.refresh(user)
        assert user.founding_member is True
        assert user.founding_slot_number == 1
        assert user.subscription_tier == "pro"
        assert user.subscription_status == "active"
        assert user.subscription_current_period_end is None
        assert user.subscription_id is None

    @patch("app.subscription_service.stripe")
    def test_webhook_replay_same_event_id_is_noop(self, mock_stripe, db_session):
        """A genuine Stripe redelivery of the SAME event_id: record_event_or_skip
        (called by the real /api/stripe/webhook route, exercised here via
        handle_checkout_completed called twice — the dedup table is the
        route's job, but the grant function must ALSO be replay-safe for a
        second webhook after the first one already seated the user, which is
        exactly what this test proves)."""
        from app.subscription_service import handle_checkout_completed

        user = _make_user(db_session)
        session_data = _fake_founding_checkout_session(user_id=user.id)
        event = _fake_event("checkout.session.completed", **session_data)

        first = handle_checkout_completed(event, db_session)
        second = handle_checkout_completed(event, db_session)

        assert first["granted"] is True
        assert second["granted"] is False
        assert second["replay"] is True
        db_session.refresh(user)
        assert user.founding_slot_number == 1  # unchanged — no double-grant

    @patch("app.subscription_service.stripe")
    def test_webhook_replay_via_dedup_table_returns_already_processed(self, mock_stripe, client, db_session):
        """Full webhook route: replaying the exact event_id short-circuits at
        record_event_or_skip and never re-invokes the founding grant at all."""
        from app.creator_routes import verify_webhook_signature

        user = _make_user(db_session)
        session_data = _fake_founding_checkout_session(user_id=user.id)
        event = _fake_event("checkout.session.completed", event_id="evt_founding_replay", **session_data)

        with patch("app.creator_routes.verify_webhook_signature", return_value=event):
            r1 = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=fake"})
            r2 = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=fake"})

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json()["already_processed"] is True

    def test_non_founding_payment_session_still_skips_non_subscription(self, db_session):
        """Regression pin (Trap 4): a plain mode=payment session with NO
        founding metadata must still hit the ORIGINAL skip path unchanged."""
        from app.subscription_service import handle_checkout_completed

        event = _fake_event(
            "checkout.session.completed",
            id="cs_plain_payment",
            mode="payment",
            payment_status="paid",
            metadata={},
        )
        result = handle_checkout_completed(event, db_session)
        assert result == {"skipped": "non-subscription session"}

    def test_subscription_session_unaffected_by_founding_routing(self, db_session):
        """A normal mode=subscription session is untouched by the new branch."""
        from app.subscription_service import handle_checkout_completed

        event = _fake_event(
            "checkout.session.completed",
            id="cs_sub",
            mode="subscription",
            payment_status="unpaid",
            metadata={},
        )
        result = handle_checkout_completed(event, db_session)
        assert result == {"skipped": "payment_status=unpaid"}


# ── 2 & 4: grant idempotency + shape parity with grant_comp_tier.py ─────


class TestGrantFoundingMembership:
    def test_grant_is_idempotent_replay(self, db_session):
        from app.services.founding_service import grant_founding_membership

        user = _make_user(db_session)
        first = grant_founding_membership(user, db_session)
        second = grant_founding_membership(user, db_session)

        assert first == {"granted": True, "replay": False, "slot": 1}
        assert second == {"granted": False, "replay": True, "slot": 1}

    def test_grant_shape_matches_comp_tier_script(self, db_session):
        """The four fields grant_comp_tier.py --apply writes for a comp'd
        'pro' grant: subscription_tier='pro', subscription_status='active',
        subscription_current_period_end=None, subscription_id=None."""
        from app.services.founding_service import grant_founding_membership

        user = _make_user(db_session)
        grant_founding_membership(user, db_session)
        db_session.refresh(user)

        assert user.subscription_tier == "pro"
        assert user.subscription_status == "active"
        assert user.subscription_current_period_end is None
        assert user.subscription_id is None
        # gates on status in (active, trialing) — same PASS check
        # grant_comp_tier.py prints after --apply.
        assert user.subscription_status in ("active", "trialing")

    def test_grant_sold_out_raises_and_leaves_user_untouched(self, db_session):
        from app.services.founding_service import (
            FoundingSoldOutError,
            grant_founding_membership,
        )

        with patch("app.services.founding_service.founding_slot_cap", return_value=1):
            u1 = _make_user(db_session)
            u2 = _make_user(db_session)
            grant_founding_membership(u1, db_session)
            with pytest.raises(FoundingSoldOutError):
                grant_founding_membership(u2, db_session)
        db_session.refresh(u2)
        assert u2.founding_member is False
        assert u2.founding_slot_number is None


# ── 3: cap enforcement under real concurrency ───────────────────────────


class TestConcurrencyCapEnforcement:
    def test_concurrent_grants_never_exceed_cap(self, tmp_path):
        """20 threads race for a cap of 5 seats — real commits, separate
        connections, real UNIQUE-constraint collisions. Proves the
        MAX(slot)+1-under-commit design (not an advisory pre-check) holds
        the invariant exactly, the same proof shape as
        test_agentreg_0819_agent_self_registration.py::test_concurrent_reservations_never_exceed_the_cap.
        """
        from app.models import Base
        from app.services.founding_service import (
            FoundingSoldOutError,
            grant_founding_membership,
        )

        engine = create_engine(f"sqlite:///{tmp_path / 'founding_race.db'}")
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(bind=engine)

        setup = SessionFactory()
        uids = []
        for n in range(20):
            u = User(email=f"race{n}@founding.local", display_name=f"race{n}")
            setup.add(u)
            setup.flush()
            uids.append(u.id)
        setup.commit()
        setup.close()

        cap = 5
        granted = []
        refused = []
        lock = threading.Lock()

        def worker(uid) -> None:
            session = SessionFactory()
            try:
                user = session.query(User).filter(User.id == uid).first()
                try:
                    grant_founding_membership(user, session)
                    with lock:
                        granted.append(uid)
                except FoundingSoldOutError:
                    with lock:
                        refused.append(uid)
            finally:
                session.close()

        # Patch ONCE, OUTSIDE the thread pool — mock.patch as a context
        # manager mutates a shared module attribute and is not safe to
        # enter/exit independently from N concurrent threads (a thread's
        # __exit__ restoring the original value can race a sibling thread's
        # __enter__, permanently corrupting founding_slot_cap for every test
        # that runs afterwards in the same process). One patch wrapping the
        # whole start/join block is thread-safe because no thread mutates it.
        with patch("app.services.founding_service.founding_slot_cap", return_value=cap):
            threads = [threading.Thread(target=worker, args=(uid,)) for uid in uids]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(granted) == cap, f"overshoot: {len(granted)} grants for cap {cap}"
        assert len(refused) == 20 - cap

        # Verify slot numbers are exactly {1..cap}, no gaps, no duplicates.
        verify = SessionFactory()
        slots = sorted(
            row[0]
            for row in verify.query(User.founding_slot_number).filter(User.founding_slot_number.is_not(None)).all()
        )
        verify.close()
        assert slots == list(range(1, cap + 1))


# ── 5: route shadowing regression ───────────────────────────────────────


class TestRouteShadowing:
    def test_founding_checkout_route_reachable_not_swallowed_by_tier_route(self, client, db_session):
        """POST /api/checkout/founding must reach the dedicated founding
        handler, not the {tier} handler (which would 400 invalid_tier)."""
        from app.checkout_routes import get_current_user_optional

        user = _make_user(db_session, stripe_customer_id="cus_route_test")
        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            with patch("app.services.founding_service.stripe") as mock_stripe:
                mock_stripe.checkout.Session.create.return_value = {
                    "id": "cs_founding_route",
                    "url": "https://checkout.stripe.com/founding",
                }
                with patch("app.subscription_service.get_or_create_customer", return_value="cus_route_test"):
                    with patch(
                        "app.services.founding_service.founding_price_id",
                        return_value="price_test_founding",
                    ):
                        resp = client.post("/api/checkout/founding")
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "founding"
        assert "invalid_tier" not in str(data)

    def test_tier_route_rejects_founding_as_belt_and_suspenders(self, client, db_session):
        """Even calling the {tier} pattern directly with tier=founding must
        404 with the redirect hint, never fall through to invalid_tier."""
        from app.checkout_routes import get_current_user_optional

        user = _make_user(db_session)
        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            # This exercises the SAME route FastAPI would dispatch to if the
            # static /checkout/founding route were ever removed/reordered —
            # simulated here by calling create_subscription_checkout directly
            # is unnecessary; the live route registration order already
            # sends /checkout/founding to the static handler, so this proves
            # the inline guard fires if that ever regresses.
            from app.checkout_routes import create_subscription_checkout
            import asyncio
            from fastapi import Request

            scope = {"type": "http", "method": "POST", "headers": []}
            request = Request(scope)
            with pytest.raises(Exception) as exc_info:
                asyncio.run(create_subscription_checkout("founding", request, db_session, user))
            assert "use_founding_endpoint" in str(exc_info.value)
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)

    def test_founding_checkout_requires_auth(self, client):
        resp = client.post("/api/checkout/founding")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "login_required"

    def test_founding_checkout_409_when_sold_out(self, client, db_session):
        from app.checkout_routes import get_current_user_optional

        user = _make_user(db_session)
        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            with patch("app.services.founding_service.founding_seats_remaining", return_value=0):
                resp = client.post("/api/checkout/founding")
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)
        assert resp.status_code == 409


# ── 7: public remaining-seats endpoint ──────────────────────────────────


class TestPublicRemainingEndpoint:
    def test_remaining_endpoint_shape(self, client, db_session):
        resp = client.get("/api/founding/remaining")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["cap"] == 100
        assert data["price_usd"] == 49
        assert data["remaining"] == 100

    def test_remaining_decrements_after_grant(self, client, db_session):
        from app.services.founding_service import grant_founding_membership

        user = _make_user(db_session)
        grant_founding_membership(user, db_session)

        resp = client.get("/api/founding/remaining")
        assert resp.json()["remaining"] == 99

    def test_remaining_fails_closed_when_unconfigured(self, client):
        with patch("app.services.founding_service._load_founding_config", return_value={}):
            resp = client.get("/api/founding/remaining")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["remaining"] == 0


# ── 8: SSOT guard — no bare literal outside tiers.yaml ──────────────────


class TestSsotGuard:
    """price 49 and cap 100 must appear as NUMBER tokens ONLY in
    config/tiers.yaml — never re-hardcoded in the service/route modules.
    Comments/docstrings are naturally excluded by tokenizing (not
    string-grepping), so a docstring mentioning "$49" or "100 seats" cannot
    false-positive this guard.
    """

    @pytest.mark.parametrize(
        "relpath",
        [
            "app/services/founding_service.py",
            "app/checkout_routes.py",
        ],
    )
    def test_no_bare_price_or_cap_literal(self, relpath):
        repo_root = Path(__file__).resolve().parent.parent
        path = repo_root / relpath
        with open(path, "rb") as f:
            tokens = list(tokenize.tokenize(f.readline))
        numbers = {t.string for t in tokens if t.type == tokenize.NUMBER}
        assert "49" not in numbers, f"{relpath} hardcodes the founding price outside tiers.yaml"
        assert "100" not in numbers, f"{relpath} hardcodes the founding cap outside tiers.yaml"


# ── 9: lost-race auto-refund ─────────────────────────────────────────────


class TestLostRaceRefund:
    @patch("app.subscription_service.stripe")
    def test_webhook_refunds_on_lost_cap_race(self, mock_stripe, db_session):
        from app.subscription_service import handle_checkout_completed

        with patch("app.services.founding_service.founding_slot_cap", return_value=1):
            seated = _make_user(db_session)
            from app.services.founding_service import grant_founding_membership

            grant_founding_membership(seated, db_session)  # takes the only seat

            loser = _make_user(db_session)
            session_data = _fake_founding_checkout_session(
                session_id="cs_loser", user_id=loser.id, pi="pi_loser"
            )
            event = _fake_event("checkout.session.completed", event_id="evt_loser", **session_data)

            with patch("app.services.founding_service.stripe") as mock_founding_stripe:
                result = handle_checkout_completed(event, db_session)

        assert result.get("refunded") is True
        mock_founding_stripe.Refund.create.assert_called_once()
        _, kwargs = mock_founding_stripe.Refund.create.call_args
        assert kwargs["payment_intent"] == "pi_loser"
        assert "founding_soldout_refund_pi_loser" in kwargs["idempotency_key"]

    def test_refund_never_raises_on_stripe_error(self):
        from app.services.founding_service import refund_lost_race

        with patch("app.services.founding_service.stripe") as mock_stripe:
            mock_stripe.Refund.create.side_effect = RuntimeError("stripe down")
            refund_lost_race("pi_whatever")  # must not raise

    def test_refund_noop_on_missing_payment_intent(self):
        from app.services.founding_service import refund_lost_race

        with patch("app.services.founding_service.stripe") as mock_stripe:
            refund_lost_race(None)
            mock_stripe.Refund.create.assert_not_called()
