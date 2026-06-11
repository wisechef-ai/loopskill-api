"""Tests for GET /api/admin/pulse — the north-star demand scoreboard.

The pulse endpoint is the "one number" the khaserto GTM loop reads each morning:
paying operators, MRR, free-sync paywall pressure, and fleet-deploy activity.
Distinct from /api/stats (supply-side vanity). Master-key gated.

Covers:
  - MRR + paying-operator math across pro / pro_plus / legacy cook
  - canceled / past_due subscriptions are NOT counted as paying
  - free-sync paywall-pressure counters (total + 7d)
  - fleet + fleet-subscription deploy counters (total + 7d)
  - the master-key gate (a normal user api key → 403)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import get_db
from app.models import Fleet, FleetSubscription, User


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


def _user(
    db: Session,
    *,
    tier: str | None,
    status: str | None = "active",
    free_sync_used_at: datetime | None = None,
) -> User:
    uid = uuid4()
    u = User(
        id=uid,
        display_name="Op",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status=status,
        free_sync_used_at=free_sync_used_at,
    )
    db.add(u)
    db.flush()
    return u


def _fleet(db: Session, *, owner_id, created_at: datetime | None = None) -> Fleet:
    f = Fleet(id=uuid4(), owner_user_id=owner_id, name="F", fleet_api_key_hash=uuid4().hex)
    if created_at is not None:
        f.created_at = created_at
    db.add(f)
    db.flush()
    return f


class TestAdminPulse:
    def test_pulse_counts_paying_operators_and_mrr(self, db_session):
        now = datetime.now(UTC)
        old = now - timedelta(days=30)
        recent = now - timedelta(days=2)

        # Paying: 2 pro ($20), 1 pro_plus ($100), 1 legacy cook (→pro, $20) = $160, 4 operators
        _user(db_session, tier="pro")
        _user(db_session, tier="pro")
        _user(db_session, tier="pro_plus")
        _user(db_session, tier="cook")  # legacy alias → pro
        # NOT paying: free, canceled-pro, past_due-pro, null-tier
        _user(db_session, tier="free")
        _user(db_session, tier="pro", status="canceled")
        _user(db_session, tier="pro", status="past_due")
        _user(db_session, tier=None, status=None)
        # Free-sync pressure: 1 old, 2 recent (within 7d)
        _user(db_session, tier="free", free_sync_used_at=old)
        _user(db_session, tier="free", free_sync_used_at=recent)
        owner = _user(db_session, tier="free", free_sync_used_at=recent)
        db_session.commit()

        # Fleet deploy activity: 1 fleet, 2 subs (1 old, 1 recent)
        fleet = _fleet(db_session, owner_id=owner.id)
        from app.models import Cookbook

        cb1 = Cookbook(id=uuid4(), name="cb1", cookbook_owner=owner.id)
        cb2 = Cookbook(id=uuid4(), name="cb2", cookbook_owner=owner.id)
        db_session.add_all([cb1, cb2])
        db_session.flush()
        s1 = FleetSubscription(fleet_id=fleet.id, cookbook_id=cb1.id, channel="stable")
        s1.subscribed_at = old
        s2 = FleetSubscription(fleet_id=fleet.id, cookbook_id=cb2.id, channel="stable")
        s2.subscribed_at = recent
        db_session.add_all([s1, s2])
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=None, is_admin=True)
        with TestClient(app) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["paying_operators"] == 4
        assert body["mrr_usd"] == 160  # 2*20 + 1*100 + 1*20
        assert body["by_tier"] == {"pro": 3, "pro_plus": 1}  # cook folded into pro
        assert body["free_sync_used_total"] == 3
        assert body["free_sync_used_7d"] == 2
        assert body["fleets_total"] == 1
        assert body["fleet_subscriptions_total"] == 2
        assert body["fleet_subscriptions_7d"] == 1
        assert "generated_at" in body

    def test_pulse_zero_state(self, db_session):
        app = _make_app(db_session, api_key_user_id=None, is_admin=True)
        with TestClient(app) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["paying_operators"] == 0
        assert body["mrr_usd"] == 0
        assert body["by_tier"] == {}
        assert body["fleets_total"] == 0

    def test_pulse_requires_master_key(self, db_session):
        # A normal user api key (api_key_user_id set) must be rejected.
        app = _make_app(db_session, api_key_user_id=uuid4(), is_admin=False)
        with TestClient(app) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 403, r.text
