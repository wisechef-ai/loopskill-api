"""Regression: fresh-OAuth wr_jwt cookie must authenticate GET /api/fleets and
GET /api/cookbooks the same way it authenticates GET /api/auth/me.

Bug history
-----------
2026-07-19 17:01:00 UTC prod incident: a browser completed Google OAuth at
17:00:51 (callback set a fresh, valid wr_jwt cookie), then in the same
second: GET /api/auth/me → 200, GET /api/fleets → 401, GET /api/cookbooks →
401. Portal redirect-looped between login and logout.

Root cause
----------
``_auth_ctx_from_jwt_cookie`` (app/middleware/_token_auth.py) gated identity
resolution on ``user.subscription_status in ("active", "trialing")`` — i.e.
it conflated "is this a valid authenticated user" with "does this user have
a paid subscription". A brand-new OAuth user has ``subscription_status`` and
``subscription_tier`` both NULL (no DB default, no subscription yet), so the
helper returned ``AuthContext.anonymous()`` for ANY fresh signup — not just
this one incident. ``_try_jwt_cookie_auth`` then sees ``scope != "user"`` and
returns False, and ``APIKeyMiddleware.dispatch`` 401s with "Invalid or
missing x-api-key header".

Meanwhile ``/api/auth/me`` (app/auth_routes.py:get_me) has NO subscription
check at all — it decodes the JWT, looks the user up by id, and returns 200
unconditionally. Hence the split-brain: same cookie, same second, one path
200s and the other two 401.

The sibling resolver in the same file, ``_auth_ctx_from_api_key``, does NOT
have this bug — it always returns a "user"-scope AuthContext for a valid
key, and only conditionally sets `tier` based on subscription status. That
is the correct pattern this fix aligns the cookie path to.

Contract pinned
----------------
1. A fresh (subscription_status=None, subscription_tier=None) user with a
   valid wr_jwt cookie must get 200 from /api/fleets and /api/cookbooks —
   NOT the "x-api-key" middleware rejection.
2. /api/auth/me must also 200 for the same cookie (parity check).
3. No cookie / invalid cookie still 401s (anonymous stays out).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, User


@pytest.fixture()
def db_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jwtparity.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def fresh_user_app(db_engine, monkeypatch):
    """A user who JUST completed OAuth — no subscription row/status yet."""
    from tests._app_factory import build_test_app

    SessionLocal = sessionmaker(bind=db_engine, future=True)
    session = SessionLocal()

    uid = uuid.uuid4()
    user = User(
        id=uid,
        email="fresh-oauth@test.local",
        display_name="Fresh OAuth User",
        google_id=str(uuid.uuid4().int)[:16],
        # Deliberately NOT set — mirrors a brand-new OAuth signup with no
        # Stripe subscription yet. This is the exact prod state that 401'd.
        subscription_tier=None,
        subscription_status=None,
    )
    session.add(user)
    session.commit()

    from app.config import settings

    monkeypatch.setattr(settings, "API_KEY", "rec_admin_master_xyz_1234", raising=False)

    app = build_test_app(db_session=session, monkeypatch=monkeypatch)
    try:
        yield app, user
    finally:
        session.close()


def _jwt_for(user) -> str:
    from app.auth import create_jwt

    return create_jwt(user)


def test_auth_me_200_for_fresh_oauth_cookie(fresh_user_app):
    """Baseline: /api/auth/me already works for a fresh cookie (never broken)."""
    app, user = fresh_user_app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/api/auth/me", cookies={"wr_jwt": _jwt_for(user)})

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text!r}"
    assert resp.json()["id"] == str(user.id)


def test_fleets_200_for_fresh_oauth_cookie(fresh_user_app):
    """THE BUG: /api/fleets must 200 for the same cookie, same user, same moment."""
    app, user = fresh_user_app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/api/fleets", cookies={"wr_jwt": _jwt_for(user)})

    assert "x-api-key" not in resp.text.lower(), f"JWT-cookie user got the API-key gate error: {resp.text!r}"
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text!r}"


def test_cookbooks_200_for_fresh_oauth_cookie(fresh_user_app):
    """THE BUG: /api/cookbooks must 200 for the same cookie, same user, same moment."""
    app, user = fresh_user_app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/api/cookbooks", cookies={"wr_jwt": _jwt_for(user)})

    assert "x-api-key" not in resp.text.lower(), f"JWT-cookie user got the API-key gate error: {resp.text!r}"
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text!r}"


def test_no_auth_still_401s_fleets(fresh_user_app):
    """No cookie and no key → middleware still rejects (security posture unchanged)."""
    app, _user = fresh_user_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/fleets")
    assert resp.status_code == 401
    assert "x-api-key" in resp.text.lower()


def test_invalid_jwt_cookie_still_401s_fleets(fresh_user_app):
    """A garbage wr_jwt cookie must not pass — falls back to 401."""
    app, _user = fresh_user_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/fleets", cookies={"wr_jwt": "not.a.real.jwt"})
    assert resp.status_code == 401
