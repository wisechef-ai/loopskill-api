"""livefix_0601 — JWT cookie must authenticate non-allowlisted authed routes.

Bug history
-----------
The /cookbooks/view portal page calls GET /api/cookbooks/{id}. Portal/OAuth
users authenticate with a ``wr_jwt`` cookie, NOT an ``x-api-key`` header.
``APIKeyMiddleware`` bare-401'd ("Invalid or missing x-api-key header") on any
authed route when no x-api-key was present — it never consulted the JWT cookie
outside the public-skill-detail and /api/me allowlists. Result: a logged-in
Pro/Pro+ user could not load their own cookbook; the page dead-ended on
"Connect your API key".

Contract pinned
---------------
1. A valid wr_jwt cookie (active/trialing sub) on /api/cookbooks/{id} must NOT
   surface the "x-api-key" middleware error — it passes through to the route,
   which stamps api_key_user_id from the cookie.
2. No cookie + no key → still 401 (anonymous stays out).
3. Admin routes (api_key_user_id-must-be-None gate) must still 403 a cookie
   user — the cookie is user-scope, never master. No privilege escalation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Cookbook, User


@pytest.fixture()
def db_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jwtcookie.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def app_and_data(db_engine, monkeypatch):
    from tests._app_factory import build_test_app

    SessionLocal = sessionmaker(bind=db_engine, future=True)
    session = SessionLocal()

    uid = uuid.uuid4()
    user = User(
        id=uid,
        email="pro@test.local",
        display_name="Pro User",
        github_id=int(uuid.uuid4().int) % 9_000_000 + 1_000_000,
        subscription_tier="pro",
        subscription_status="active",
    )
    session.add(user)

    cb = Cookbook(
        id=uuid.uuid4(),
        cookbook_owner=uid,
        name="My Cookbook",
        created_at=datetime.now(timezone.utc),
    )
    session.add(cb)
    session.commit()

    from app.config import settings
    monkeypatch.setattr(settings, "API_KEY", "rec_admin_master_xyz_1234", raising=False)

    app = build_test_app(db_session=session, monkeypatch=monkeypatch)
    try:
        yield app, user, cb
    finally:
        session.close()


def _jwt_for(user) -> str:
    from app.auth import create_jwt
    return create_jwt(user)


def test_jwt_cookie_authenticates_cookbook_route(app_and_data):
    """A valid wr_jwt cookie must reach GET /api/cookbooks/{id} (no x-api-key)."""
    app, user, cb = app_and_data
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get(f"/api/cookbooks/{cb.id}", cookies={"wr_jwt": _jwt_for(user)})

    # Must NOT be the middleware's bare x-api-key rejection.
    assert "x-api-key" not in resp.text.lower(), (
        f"JWT-cookie user got the API-key gate error: {resp.text!r}"
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text!r}"
    body = resp.json()
    assert body["name"] == "My Cookbook"


def test_no_auth_still_401s_cookbook_route(app_and_data):
    """No cookie and no key → middleware still rejects."""
    app, user, cb = app_and_data
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/api/cookbooks/{cb.id}")
    assert resp.status_code == 401
    assert "x-api-key" in resp.text.lower()


def test_invalid_jwt_cookie_does_not_authenticate(app_and_data):
    """A garbage wr_jwt cookie must not pass — falls back to 401."""
    app, user, cb = app_and_data
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/api/cookbooks/{cb.id}", cookies={"wr_jwt": "not.a.real.jwt"})
    assert resp.status_code == 401


def test_jwt_cookie_user_cannot_reach_admin_route(app_and_data):
    """No privilege escalation: cookie user is user-scope, admin routes 403."""
    app, user, cb = app_and_data
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/admin/reindex-all", cookies={"wr_jwt": _jwt_for(user)})
    # api_key_user_id is stamped with the real UUID (never None=master) → 403.
    assert resp.status_code == 403, f"cookie user must NOT reach admin: {resp.text!r}"
