"""Tests for GET /api/admin/pulse — the north-star demand scoreboard.

The pulse endpoint is the "one number" the khaserto GTM loop reads each morning.
Its headline is REAL CASH MRR (net of promo discounts), NOT list-price ×
subscriber count — because a 100%-off promo "customer" pays $0 and our DB can't
tell the difference locally (it stores only tier+status). The truth lives in
Stripe, so the cash figures are resolved there and degrade to None (never
list-price) when Stripe is unreachable.

Covers:
  - _monthly_cents_from_stripe_sub: the pure discount-math core (no network)
  - the master-key gate (a normal user api key -> 403)
  - DB-only context fields (active subs, by_tier, list ceiling, fleet counters)
  - the no-Stripe-customers fast path ($0 real cash, no API call)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.admin_routes import _monthly_cents_from_stripe_sub
from app.database import get_db
from app.models import Fleet, FleetSubscription, User


# ─────────────── Pure discount-math core (no network) ───────────────


def _sub(unit_amount, *, interval="month", qty=1, percent_off=None, amount_off=None):
    """Build a minimal Stripe-subscription-shaped dict for the pure function."""
    s: dict = {
        "items": {"data": [{"quantity": qty, "price": {"unit_amount": unit_amount, "recurring": {"interval": interval, "interval_count": 1}}}]},
    }
    if percent_off is not None or amount_off is not None:
        s["discount"] = {"coupon": {"percent_off": percent_off, "amount_off": amount_off}}
    return s


class TestMonthlyCentsPure:
    def test_full_price_monthly(self):
        # $20.00/mo, no discount → 2000 cents
        assert _monthly_cents_from_stripe_sub(_sub(2000)) == 2000

    def test_hundred_percent_off_is_zero(self):
        # The promo-code illusion: 100%-off → real cash is $0, NOT list price.
        assert _monthly_cents_from_stripe_sub(_sub(2000, percent_off=100)) == 0

    def test_fifty_percent_off(self):
        assert _monthly_cents_from_stripe_sub(_sub(2000, percent_off=50)) == 1000

    def test_amount_off(self):
        # $100.00 list, $100.00 off → 0
        assert _monthly_cents_from_stripe_sub(_sub(10000, amount_off=10000)) == 0
        # $100.00 list, $30.00 off → 7000
        assert _monthly_cents_from_stripe_sub(_sub(10000, amount_off=3000)) == 7000

    def test_yearly_normalised_to_monthly(self):
        # $240/yr → $20/mo
        assert _monthly_cents_from_stripe_sub(_sub(24000, interval="year")) == 2000

    def test_quantity_multiplies(self):
        assert _monthly_cents_from_stripe_sub(_sub(2000, qty=3)) == 6000

    def test_discount_cannot_go_negative(self):
        assert _monthly_cents_from_stripe_sub(_sub(2000, amount_off=999999)) == 0

    def test_metered_price_contributes_zero(self):
        s = {"items": {"data": [{"price": {"unit_amount": None, "recurring": {"interval": "month"}}}]}}
        assert _monthly_cents_from_stripe_sub(s) == 0

    def test_empty_subscription(self):
        assert _monthly_cents_from_stripe_sub({}) == 0


# ─────────────── Endpoint (DB-only paths, no Stripe) ───────────────


def _make_app(db: Session, *, api_key_user_id, is_admin: bool) -> FastAPI:
    from app.admin_routes import router as admin_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    _uid = api_key_user_id

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = None if is_admin else _uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(admin_router)
    return app


def _user(db, *, tier, status="active", stripe_customer_id=None, free_sync_used_at=None):
    uid = uuid4()
    u = User(
        id=uid,
        display_name="Op",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status=status,
        stripe_customer_id=stripe_customer_id,
        free_sync_used_at=free_sync_used_at,
    )
    db.add(u)
    db.flush()
    return u


class TestAdminPulseEndpoint:
    def test_no_stripe_customers_is_zero_real_cash(self, db_session):
        """Active subs but NO stripe_customer_id → real cash $0 without any API call.

        This is the cold-start reality: subscribers exist (e.g. comped via promo
        before Stripe customer linkage) but real cash MRR is unambiguously $0.
        """
        now = datetime.now(UTC)
        _user(db_session, tier="pro_plus")  # no stripe_customer_id
        _user(db_session, tier="pro")
        _user(db_session, tier="free", free_sync_used_at=now - timedelta(days=2))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=None, is_admin=True)
        with TestClient(app) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["mrr_source"] == "stripe"
        assert b["real_cash_mrr_usd"] == 0  # the HONEST number
        assert b["paying_operators"] == 0
        assert b["active_subscriptions"] == 2  # pro_plus + pro
        assert b["comped_subscriptions"] == 2  # both active, neither pays
        assert b["by_tier"] == {"pro_plus": 1, "pro": 1}
        assert b["list_mrr_ceiling_usd"] == 120  # 100 + 20, labeled a CEILING not revenue
        assert b["free_sync_used_7d"] == 1

    def test_zero_state(self, db_session):
        app = _make_app(db_session, api_key_user_id=None, is_admin=True)
        with TestClient(app) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["real_cash_mrr_usd"] == 0
        assert b["paying_operators"] == 0
        assert b["active_subscriptions"] == 0
        assert b["by_tier"] == {}
        assert b["fleet_subscriptions_total"] == 0

    def test_fleet_deploy_counters(self, db_session):
        from app.models import Bundle

        owner = _user(db_session, tier="free")
        db_session.commit()
        fleet = Fleet(id=uuid4(), owner_user_id=owner.id, name="F", fleet_api_key_hash=uuid4().hex)
        db_session.add(fleet)
        db_session.flush()
        cb = Bundle(id=uuid4(), name="cb", bundle_owner=owner.id)
        db_session.add(cb)
        db_session.flush()
        sub = FleetSubscription(fleet_id=fleet.id, bundle_id=cb.id, channel="stable")
        sub.subscribed_at = datetime.now(UTC) - timedelta(days=1)
        db_session.add(sub)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=None, is_admin=True)
        with TestClient(app) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["fleets_total"] == 1
        assert b["fleet_subscriptions_total"] == 1
        assert b["fleet_subscriptions_7d"] == 1

    def test_requires_master_key(self, db_session):
        app = _make_app(db_session, api_key_user_id=uuid4(), is_admin=False)
        with TestClient(app) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 403, r.text
