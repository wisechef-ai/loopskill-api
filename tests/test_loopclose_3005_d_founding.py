"""loopclose_3005 Phase D — Founding Integrator SKU tests.

Covers the one-time founding payment rail end-to-end at the unit/integration
level (Stripe is mocked — the live-rail probe is a manual deploy step):

SSOT reads
  - founding config loads from config/tiers.yaml; $1000 + 25-cap have no
    second source in code
Seat counter
  - seats_taken counts founding_slot_number, remaining never negative
Checkout (POST /api/checkout/founding)
  - 401 anon, 503 not-configured, 409 sold-out, 409 already-member, 200 happy
Webhook grant (handle_checkout_completed → _handle_founding_completed)
  - mode=payment + kind=founding grants lifetime pro_plus + assigns seat #1
  - replay of the same paid event is idempotent (no double seat)
  - a plain mode=payment session with NO founding metadata is still skipped
    (regression-pin on the pre-existing behavior)
Over-sell guard
  - grant refuses once slot_cap seats exist (FoundingSoldOutError)
  - the unique constraint on founding_slot_number rejects a duplicate seat
Status endpoint (GET /api/founding/status)
  - reports enabled/cap/taken/remaining/sold_out
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import User


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session, email: str = "founder@example.com", **kwargs) -> User:
    defaults = dict(
        display_name="Founder TestUser",
        subscription_status=None,
        subscription_tier=None,
    )
    defaults.update(kwargs)
    user = User(id=uuid4(), email=email, **defaults)
    db.add(user)
    db.flush()
    return user


def _founding_session(user_id: str, session_id: str = "cs_founding_1",
                      payment_status: str = "paid", payment_intent: str = "pi_founding_1") -> dict:
    return {
        "id": session_id,
        "mode": "payment",
        "payment_status": payment_status,
        "customer": "cus_founding_1",
        "payment_intent": payment_intent,
        "metadata": {"kind": "founding", "wiserecipes_user_id": user_id},
    }


def _event(session: dict, event_id: str = "evt_founding_1") -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": session},
    }


# ── SSOT config ──────────────────────────────────────────────────────────


class TestFoundingSSOT:
    def test_slot_cap_and_price_from_yaml(self):
        """The cap and price come from config/tiers.yaml, nowhere else."""
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        assert fs.founding_slot_cap() == 25
        assert fs.founding_price_usd() == 1000
        assert fs.founding_grant_tier() == "pro_plus"

    def test_no_second_copy_of_cap_or_price_in_code(self):
        """Guard: the literals 25 (cap) / 1000 (price) live only in tiers.yaml.

        Tokenizes the founding service + checkout route and asserts neither
        number appears as a NUMBER token in executable code (comments and
        string/docstring contents are excluded by the tokenizer). This is the
        'no second number survives outside SSOT' enforcement.
        """
        import pathlib
        import tokenize

        root = pathlib.Path(__file__).resolve().parent.parent
        for rel in ("app/founding_service.py", "app/checkout_routes.py"):
            path = root / rel
            number_tokens: list[str] = []
            with open(path, "rb") as fh:
                for tok in tokenize.tokenize(fh.readline):
                    if tok.type == tokenize.NUMBER:
                        number_tokens.append(tok.string)
            assert "25" not in number_tokens, f"hardcoded cap 25 in {rel}"
            assert "1000" not in number_tokens, f"hardcoded price 1000 in {rel}"

    def test_price_id_resolves_from_env(self, monkeypatch):
        from app import founding_service as fs
        from app.config import settings

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "price_live_founding_xyz")
        assert fs.founding_price_id() == "price_live_founding_xyz"
        assert fs.founding_enabled() is True

    def test_not_enabled_when_price_unset(self, monkeypatch):
        from app import founding_service as fs
        from app.config import settings

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "")
        assert fs.founding_price_id() == ""
        assert fs.founding_enabled() is False


# ── Seat counter ─────────────────────────────────────────────────────────


class TestSeatCounter:
    def test_seats_taken_counts_slot_number(self, db_session):
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        assert fs.founding_seats_taken(db_session) == 0
        _make_user(db_session, email="f1@x.com", founding_member=True, founding_slot_number=1)
        _make_user(db_session, email="f2@x.com", founding_member=True, founding_slot_number=2)
        db_session.flush()
        assert fs.founding_seats_taken(db_session) == 2
        assert fs.founding_seats_remaining(db_session) == 23

    def test_remaining_never_negative(self, db_session, monkeypatch):
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        # Force a tiny cap and exceed it.
        monkeypatch.setattr(fs, "founding_slot_cap", lambda: 1)
        _make_user(db_session, email="f1@x.com", founding_member=True, founding_slot_number=1)
        _make_user(db_session, email="f2@x.com", founding_member=True, founding_slot_number=2)
        db_session.flush()
        assert fs.founding_seats_remaining(db_session) == 0
        assert fs.founding_is_sold_out(db_session) is True


# ── Webhook grant ────────────────────────────────────────────────────────


class TestFoundingWebhookGrant:
    def test_paid_founding_session_grants_lifetime_pro_plus(self, db_session):
        from app.subscription_service import handle_checkout_completed

        from app import founding_service as fs
        fs._founding_cfg.cache_clear()

        user = _make_user(db_session)
        event = _event(_founding_session(str(user.id)))

        result = handle_checkout_completed(event, db_session)
        assert result["processed"] == "founding"
        assert result["granted"] is True
        assert result["slot_number"] == 1
        db_session.refresh(user)
        assert user.founding_member is True
        assert user.founding_slot_number == 1
        assert user.subscription_tier == "pro_plus"
        assert user.subscription_status == "active"
        # One-time payment → NO recurring subscription id
        assert user.subscription_id is None

    def test_replay_is_idempotent(self, db_session):
        """Replaying the same paid founding event must not assign a 2nd seat."""
        from app.subscription_service import handle_checkout_completed

        from app import founding_service as fs
        fs._founding_cfg.cache_clear()

        user = _make_user(db_session)
        event = _event(_founding_session(str(user.id)))

        first = handle_checkout_completed(event, db_session)
        assert first["granted"] is True
        assert first["slot_number"] == 1

        # Same session object replayed (Stripe redelivery).
        second = handle_checkout_completed(event, db_session)
        assert second["granted"] is False
        assert second["replay"] is True
        assert second["slot_number"] == 1
        db_session.refresh(user)
        assert user.founding_slot_number == 1

    def test_plain_payment_without_founding_metadata_still_skipped(self, db_session):
        """Regression-pin: a mode=payment session with no kind=founding is skipped."""
        from app.subscription_service import handle_checkout_completed

        session = {
            "id": "cs_other_1",
            "mode": "payment",
            "payment_status": "paid",
            "metadata": {},  # no kind=founding
        }
        result = handle_checkout_completed(_event(session), db_session)
        assert result.get("skipped") == "non-subscription session"

    def test_unpaid_founding_session_skipped(self, db_session):
        from app.subscription_service import handle_checkout_completed

        from app import founding_service as fs
        fs._founding_cfg.cache_clear()

        user = _make_user(db_session)
        session = _founding_session(str(user.id), payment_status="unpaid")
        result = handle_checkout_completed(_event(session), db_session)
        assert "skipped" in result
        db_session.refresh(user)
        assert user.founding_member is False


# ── Over-sell guard ──────────────────────────────────────────────────────


class TestOverSellGuard:
    def test_grant_refused_when_cap_reached(self, db_session, monkeypatch):
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(fs, "founding_slot_cap", lambda: 2)
        _make_user(db_session, email="f1@x.com", founding_member=True, founding_slot_number=1)
        _make_user(db_session, email="f2@x.com", founding_member=True, founding_slot_number=2)
        db_session.flush()

        late = _make_user(db_session, email="late@x.com")
        with pytest.raises(fs.FoundingSoldOutError):
            fs.grant_founding_membership(late, db_session)
        db_session.rollback()

    def test_sold_out_session_triggers_refund(self, db_session, monkeypatch):
        """When the grant races to sold-out, the one-time charge is refunded."""
        from app import subscription_service as ss
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(fs, "founding_slot_cap", lambda: 1)
        _make_user(db_session, email="f1@x.com", founding_member=True, founding_slot_number=1)
        db_session.flush()

        late = _make_user(db_session, email="late@x.com")
        session = _founding_session(str(late.id), session_id="cs_late", payment_intent="pi_late")

        with patch.object(ss, "stripe") as mock_stripe:
            result = ss._handle_founding_completed(session, db_session)
            assert result["founding"] == "sold_out_refunded"
            mock_stripe.Refund.create.assert_called_once()
            _, kwargs = mock_stripe.Refund.create.call_args
            assert kwargs["payment_intent"] == "pi_late"

    def test_unique_constraint_blocks_duplicate_slot(self, db_session):
        """The DB-level unique constraint on founding_slot_number is real."""
        from sqlalchemy.exc import IntegrityError

        _make_user(db_session, email="a@x.com", founding_member=True, founding_slot_number=5)
        # _make_user flushes internally, so the duplicate-slot insert raises here.
        with pytest.raises(IntegrityError):
            _make_user(db_session, email="b@x.com", founding_member=True, founding_slot_number=5)
        db_session.rollback()


# ── Checkout endpoint ────────────────────────────────────────────────────


class TestFoundingCheckoutEndpoint:
    def test_anonymous_rejected(self, client):
        resp = client.post("/api/checkout/founding")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "login_required"

    def test_not_configured_returns_503(self, client, db_session, monkeypatch):
        from app.checkout_routes import get_current_user_optional
        from app.config import settings
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "")  # not wired
        user = _make_user(db_session)
        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            resp = client.post("/api/checkout/founding")
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "founding_not_configured"

    def test_already_member_returns_409(self, client, db_session, monkeypatch):
        from app.checkout_routes import get_current_user_optional
        from app.config import settings
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "price_founding_live")
        user = _make_user(db_session, founding_member=True, founding_slot_number=3)
        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            resp = client.post("/api/checkout/founding")
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "already_founding_member"

    def test_sold_out_returns_409(self, client, db_session, monkeypatch):
        from app.checkout_routes import get_current_user_optional
        from app.config import settings
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "price_founding_live")
        monkeypatch.setattr(fs, "founding_slot_cap", lambda: 1)
        _make_user(db_session, email="seat1@x.com", founding_member=True, founding_slot_number=1)
        user = _make_user(db_session)
        db_session.flush()
        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        try:
            resp = client.post("/api/checkout/founding")
        finally:
            client.app.dependency_overrides.pop(get_current_user_optional, None)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "sold_out"

    def test_happy_path_creates_session(self, client, db_session, monkeypatch):
        from app.checkout_routes import get_current_user_optional
        from app.config import settings
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "price_founding_live")
        user = _make_user(db_session, stripe_customer_id="cus_existing")

        with patch.object(fs, "stripe") as mock_stripe:
            mock_stripe.checkout.Session.create.return_value = {
                "id": "cs_founding_happy",
                "url": "https://checkout.stripe.com/founding",
            }
            client.app.dependency_overrides[get_current_user_optional] = lambda: user
            try:
                resp = client.post("/api/checkout/founding")
            finally:
                client.app.dependency_overrides.pop(get_current_user_optional, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "cs_founding_happy"
        assert data["kind"] == "founding"
        # The session must be created as a ONE-TIME payment, not a subscription.
        _, kwargs = mock_stripe.checkout.Session.create.call_args
        assert kwargs["mode"] == "payment"
        assert kwargs["metadata"]["kind"] == "founding"


# ── Status endpoint ──────────────────────────────────────────────────────


class TestFoundingStatusEndpoint:
    def test_status_reports_availability(self, client, db_session, monkeypatch):
        from app.config import settings
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "price_founding_live")
        _make_user(db_session, email="seat1@x.com", founding_member=True, founding_slot_number=1)
        db_session.flush()

        resp = client.get("/api/founding/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["slot_cap"] == 25
        assert data["seats_taken"] == 1
        assert data["seats_remaining"] == 24
        assert data["sold_out"] is False

    def test_status_disabled_when_unconfigured(self, client, monkeypatch):
        from app.config import settings
        from app import founding_service as fs

        fs._founding_cfg.cache_clear()
        monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "")
        resp = client.get("/api/founding/status")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}
