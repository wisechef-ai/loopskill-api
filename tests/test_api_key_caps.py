"""Tests for Phase C per-cookbook API key cap enforcement.

Acceptance gates from plan §3 / §6:
  - Free user: 0 active → 200; 1 active → 403 cap_exceeded
  - Pro user:  1 active → 403 (same cap as Free, both = 1)
  - Pro+ user: 20× → all 200; 21st → 403
  - POST with invalid cookbook_id → 404
  - GET /api-keys returns install_count_total + install_count_7d fields
"""
from __future__ import annotations

import hashlib
import secrets
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api_key_routes import router as api_key_router
from app.auth_routes import get_current_user_optional
from app.database import get_db
from app.models import APIKey, Base, Bundle, User


# ── In-memory DB fixture ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=e)
    yield e
    Base.metadata.drop_all(bind=e)


@pytest.fixture()
def db(engine) -> Session:
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        # Rollback any uncommitted state so tests don't bleed
        session.rollback()
        session.close()


# ── Helper factories ───────────────────────────────────────────────────────

def _make_user(db: Session, tier: str = "free", status: str = "active") -> User:
    u = User(
        id=uuid4(),
        display_name=f"Test {tier}",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_cookbook(db: Session, owner: User) -> Bundle:
    cb = Bundle(
        id=uuid4(),
        name="Test Cookbook",
        is_base=False,
        bundle_owner=owner.id,
    )
    db.add(cb)
    db.commit()
    db.refresh(cb)
    return cb


def _make_active_key(db: Session, user: User) -> APIKey:
    """Insert a pre-hashed active key for a user (does NOT go through the endpoint)."""
    body = secrets.token_urlsafe(32)
    plaintext = f"rec_live_{body}"
    prefix = plaintext[:12]
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    k = APIKey(
        id=uuid4(),
        user_id=user.id,
        key_prefix=prefix,
        key_hash=key_hash,
        name="test-key",
        label="test-key",
        is_active=True,
    )
    db.add(k)
    db.commit()
    db.refresh(k)
    return k


# ── Test app factory ───────────────────────────────────────────────────────

def _make_test_app(db: Session, authed_user: User) -> TestClient:
    """Return a TestClient wired to the given user (no auth middleware needed)."""
    app = FastAPI()
    app.include_router(api_key_router)

    def override_db():
        yield db

    def override_user():
        return authed_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user_optional] = override_user
    return TestClient(app, raise_server_exceptions=True)


# ── Free user cap tests ────────────────────────────────────────────────────

class TestFreeUserCap:
    def test_create_first_key_succeeds(self, db):
        """Free user with 0 active keys can create one."""
        user = _make_user(db, tier="free")
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"label": "first"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["label"] == "first"
        assert data["key"].startswith("rec_live_")
        assert data["bundle_id"] is None

    def test_create_second_key_blocked(self, db):
        """Free user with 1 active key cannot create another (cap=1)."""
        user = _make_user(db, tier="free")
        _make_active_key(db, user)  # pre-existing
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"label": "second"})
        assert r.status_code == 403, r.text
        assert "cap" in r.json()["detail"].lower() or "key_cap_exceeded" in r.json()["detail"]

    def test_null_tier_treated_as_free(self, db):
        """User with null subscription_tier gets free cap = 1."""
        user = _make_user(db, tier=None)
        user.subscription_tier = None
        db.commit()
        _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={})
        assert r.status_code == 403, r.text


# ── Pro user cap tests ─────────────────────────────────────────────────────

class TestProUserCap:
    def test_pro_user_gets_1_key(self, db):
        """Pro user cap is 1 (same as free)."""
        user = _make_user(db, tier="pro")
        _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"label": "second"})
        assert r.status_code == 403, r.text
        assert "key_cap_exceeded" in r.json()["detail"]

    def test_pro_first_key_ok(self, db):
        """Pro user with 0 active keys can create one."""
        user = _make_user(db, tier="pro")
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={})
        assert r.status_code == 200, r.text


# ── Pro+ user cap tests ────────────────────────────────────────────────────

class TestProPlusUserCap:
    def test_pro_plus_allows_20_keys(self, db):
        """Pro+ user can create up to 20 active keys."""
        user = _make_user(db, tier="pro_plus")
        client = _make_test_app(db, user)
        # Pre-load 19 keys directly in DB
        for i in range(19):
            _make_active_key(db, user)
        # 20th key via endpoint — should succeed
        r = client.post("/api/api-keys", json={"label": "key20"})
        assert r.status_code == 200, r.text

    def test_pro_plus_21st_key_blocked(self, db):
        """Pro+ user is blocked from 21st active key."""
        user = _make_user(db, tier="pro_plus")
        for _ in range(20):
            _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={})
        assert r.status_code == 403, r.text
        assert "key_cap_exceeded" in r.json()["detail"]


# ── Cookbook scoping tests ─────────────────────────────────────────────────

class TestCookbookScoping:
    def test_valid_cookbook_id_persisted(self, db):
        """POST with valid owned cookbook_id creates a scoped key."""
        user = _make_user(db, tier="pro_plus")
        cb = _make_cookbook(db, user)
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={
            "label": "client-key",
            "cookbook_id": str(cb.id),
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["bundle_id"] == str(cb.id)

    def test_invalid_cookbook_id_returns_404(self, db):
        """POST with cookbook_id that doesn't belong to user → 404."""
        user = _make_user(db, tier="pro_plus")
        other_user = _make_user(db, tier="pro_plus")
        cb = _make_cookbook(db, other_user)  # owned by OTHER user
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"cookbook_id": str(cb.id)})
        assert r.status_code == 404, r.text

    def test_nonexistent_cookbook_id_returns_404(self, db):
        """POST with random UUID not in DB → 404."""
        user = _make_user(db, tier="pro_plus")
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"cookbook_id": str(uuid4())})
        assert r.status_code == 404, r.text

    def test_malformed_cookbook_id_returns_400(self, db):
        """POST with non-UUID cookbook_id → 400."""
        user = _make_user(db, tier="pro_plus")
        client = _make_test_app(db, user)
        r = client.post("/api/api-keys", json={"cookbook_id": "not-a-uuid"})
        assert r.status_code == 400, r.text


# ── GET /api-keys returns install count fields ─────────────────────────────

class TestGetApiKeys:
    def test_list_includes_install_count_fields(self, db):
        """GET /api-keys must include install_count_total and install_count_7d."""
        user = _make_user(db, tier="pro")
        _make_active_key(db, user)
        client = _make_test_app(db, user)
        r = client.get("/api/api-keys")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "keys" in data
        assert len(data["keys"]) >= 1
        key_item = data["keys"][0]
        assert "install_count_total" in key_item
        assert "install_count_7d" in key_item
        assert isinstance(key_item["install_count_total"], int)
        assert isinstance(key_item["install_count_7d"], int)
        # Freshly created key has zero installs
        assert key_item["install_count_total"] == 0
        assert key_item["install_count_7d"] == 0

    def test_list_includes_cookbook_id(self, db):
        """GET /api-keys must include cookbook_id in each item."""
        user = _make_user(db, tier="pro_plus")
        cb = _make_cookbook(db, user)
        k = _make_active_key(db, user)
        k.bundle_id = cb.id
        db.commit()
        client = _make_test_app(db, user)
        r = client.get("/api/api-keys")
        assert r.status_code == 200
        keys = r.json()["keys"]
        found = next((x for x in keys if x["id"] == str(k.id)), None)
        assert found is not None
        assert found["bundle_id"] == str(cb.id)

    def test_list_includes_label(self, db):
        """GET /api-keys items include label field."""
        user = _make_user(db, tier="pro")
        k = _make_active_key(db, user)
        k.label = "my custom label"
        db.commit()
        client = _make_test_app(db, user)
        r = client.get("/api/api-keys")
        assert r.status_code == 200
        keys = r.json()["keys"]
        found = next((x for x in keys if x["id"] == str(k.id)), None)
        assert found is not None
        assert found["label"] == "my custom label"


# ── Stripe Connect 410 Gone tests ─────────────────────────────────────────

class TestStripeConnectKilled:
    """Verify that the Stripe Connect endpoints return 410 Gone."""

    @pytest.fixture()
    def creator_client(self, db):
        from app.creator_routes import router as creator_router
        app = FastAPI()
        app.include_router(creator_router)

        def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db
        return TestClient(app, raise_server_exceptions=False)

    def test_stripe_onboard_gone(self, creator_client):
        r = creator_client.post("/api/stripe/onboard", json={})
        assert r.status_code == 410, r.text
        assert "stripe_connect_removed" in r.json()["detail"]

    def test_stripe_status_gone(self, creator_client):
        r = creator_client.get("/api/stripe/status")
        assert r.status_code == 410, r.text

    def test_stripe_dashboard_gone(self, creator_client):
        r = creator_client.get("/api/stripe/dashboard")
        assert r.status_code == 410, r.text
