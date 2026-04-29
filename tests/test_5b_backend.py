"""Tests for WIS-640 (api keys) + WIS-641 (portal session) + WIS-642 (callback redirect).

In-memory SQLite + dependency_overrides — no prod DB touched.
"""
from __future__ import annotations

import uuid
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.models import APIKey, Base, User


# ── DB fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
    def _pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
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
        github_id=int(uuid.uuid4().int) % 9_000_000 + 1_000_000,
        email=f"5b-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="WIS-640 User",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _build_app(db: Session) -> FastAPI:
    from app.api_key_routes import router as api_key_router
    from app.checkout_routes import router as checkout_router

    app = FastAPI()
    def _override_get_db():
        try:
            yield db
        finally:
            pass
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(api_key_router)
    app.include_router(checkout_router)
    return app


@pytest.fixture
def authed_client(db, test_user) -> TestClient:
    from app import auth_routes
    app = _build_app(db)
    app.dependency_overrides[auth_routes.get_current_user_optional] = lambda: test_user
    return TestClient(app)


@pytest.fixture
def anon_client(db) -> TestClient:
    from app import auth_routes
    app = _build_app(db)
    app.dependency_overrides[auth_routes.get_current_user_optional] = lambda: None
    return TestClient(app)


# ── WIS-640: API key CRUD ────────────────────────────────────────────────

class TestApiKeysCreate:
    def test_creates_key_returns_plaintext_once(self, authed_client, test_user, db):
        resp = authed_client.post("/api/api-keys")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["key"].startswith("rec_live_")
        assert len(body["key"]) > 20
        assert body["prefix"].startswith("rec_live_")
        assert body["prefix"][:9] == "rec_live_"
        assert "warning" in body

        # Plaintext NOT in subsequent GET
        list_resp = authed_client.get("/api/api-keys")
        assert list_resp.status_code == 200
        keys = list_resp.json()["keys"]
        assert len(keys) == 1
        assert "key" not in keys[0]  # only prefix exposed
        assert keys[0]["prefix"].startswith("rec_live_")
        assert keys[0]["is_active"] is True

    def test_creating_second_key_revokes_first(self, authed_client, test_user, db):
        first = authed_client.post("/api/api-keys").json()
        second = authed_client.post("/api/api-keys").json()
        assert first["key"] != second["key"]

        # GET should show 2 keys total, only one active
        keys = authed_client.get("/api/api-keys").json()["keys"]
        assert len(keys) == 2
        active = [k for k in keys if k["is_active"]]
        assert len(active) == 1
        assert active[0]["id"] == second["id"]


class TestApiKeysList:
    def test_anonymous_returns_401(self, anon_client):
        assert anon_client.get("/api/api-keys").status_code == 401

    def test_returns_only_user_owned_keys(self, authed_client, test_user, db):
        # Create one for test_user
        authed_client.post("/api/api-keys")
        # Manually insert a key for a different user
        other_user = User(
            id=uuid.uuid4(),
            github_id=999_999_999,
            display_name="Other",
            email="other@test",
        )
        db.add(other_user)
        db.commit()
        db.add(APIKey(user_id=other_user.id, key_prefix="rec_live_x", key_hash="x", is_active=True))
        db.commit()

        keys = authed_client.get("/api/api-keys").json()["keys"]
        assert len(keys) == 1  # only test_user's


class TestApiKeysRevoke:
    def test_revoke_marks_inactive_idempotent(self, authed_client, db):
        created = authed_client.post("/api/api-keys").json()
        key_id = created["id"]

        r1 = authed_client.delete(f"/api/api-keys/{key_id}")
        assert r1.status_code == 200
        assert r1.json()["revoked"] is True

        # Idempotent — calling again still returns 200
        r2 = authed_client.delete(f"/api/api-keys/{key_id}")
        assert r2.status_code == 200

        # Verify status
        keys = authed_client.get("/api/api-keys").json()["keys"]
        assert all(not k["is_active"] for k in keys)

    def test_anonymous_cannot_revoke(self, anon_client, authed_client, db):
        created = authed_client.post("/api/api-keys").json()
        assert anon_client.delete(f"/api/api-keys/{created['id']}").status_code == 401

    def test_invalid_uuid_returns_400(self, authed_client):
        assert authed_client.delete("/api/api-keys/not-a-uuid").status_code == 400


# ── WIS-641: Stripe Customer Portal ──────────────────────────────────────

class TestPortalSession:
    def test_authed_with_customer_returns_url(self, authed_client, test_user, db, monkeypatch):
        test_user.stripe_customer_id = "cus_test_portal_xyz"
        db.commit()
        monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_dummy")
        monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE", "https://recipes.test/")

        with patch("stripe.billing_portal.Session.create",
                   return_value={"url": "https://billing.stripe.com/p/session/test_xyz"}) as mock:
            resp = authed_client.post("/api/billing/portal-session")

        assert resp.status_code == 200, resp.text
        assert resp.json()["url"].startswith("https://billing.stripe.com/")
        kwargs = mock.call_args.kwargs
        assert kwargs["customer"] == "cus_test_portal_xyz"
        assert kwargs["return_url"].endswith("/library")

    def test_authed_without_customer_returns_400(self, authed_client, test_user):
        # test_user has no stripe_customer_id
        resp = authed_client.post("/api/billing/portal-session")
        assert resp.status_code == 400
        assert "no_subscription" in resp.json()["detail"]

    def test_anonymous_returns_401(self, anon_client):
        assert anon_client.post("/api/billing/portal-session").status_code == 401


# ── WIS-642: OAuth callback redirect target ──────────────────────────────

class TestOAuthRedirectTarget:
    def test_success_redirect_default_targets_library(self):
        from app.auth_routes import _make_success_redirect
        resp = _make_success_redirect("dummy.jwt.token")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/library?auth=success"
        # JWT cookie set
        cookies = resp.raw_headers
        assert any(b"set-cookie" in k.lower() for k, _ in cookies)

    def test_success_redirect_honors_safe_next_url(self):
        from app.auth_routes import _make_success_redirect
        resp = _make_success_redirect("jwt", next_url="/api/checkout/cook")
        assert resp.headers["location"].startswith("/api/checkout/cook")
        assert "auth=success" in resp.headers["location"]

    def test_success_redirect_rejects_unsafe_next(self):
        from app.auth_routes import _make_success_redirect
        # Open redirect attack — external URL must NOT be honored
        resp = _make_success_redirect("jwt", next_url="https://evil.com/phish")
        assert resp.headers["location"] == "/library?auth=success"

    def test_success_redirect_rejects_relative_traversal(self):
        from app.auth_routes import _make_success_redirect
        resp = _make_success_redirect("jwt", next_url="/etc/passwd")
        assert resp.headers["location"] == "/library?auth=success"

    def test_error_redirect_targets_signin(self):
        from app.auth_routes import _make_error_redirect
        resp = _make_error_redirect("github_error")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/signin?auth=error&reason=github_error"
