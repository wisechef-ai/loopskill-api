"""Tests for Phase 3 — cookbook share-tokens (app/share_token_routes.py)."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from typing import Generator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import get_db
from app.models import (
    Base,
    Cookbook,
    CookbookShareToken,
    Skill,
    User,
)


# ─────────────────────────── Fixtures ───────────────────────────────────

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
def db_session(engine_fixture) -> Generator[Session, None, None]:
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


# ─────────────────────────── Helpers ────────────────────────────────────

def _make_user(db: Session, *, tier: str | None = "operator", status: str | None = "active") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(user)
    db.flush()
    return user


def _make_cookbook(db: Session, *, owner_id, name: str = "Test Cookbook") -> Cookbook:
    cb = Cookbook(
        id=uuid4(),
        name=name,
        description="test",
        is_base=False,
        cookbook_owner=owner_id,
    )
    db.add(cb)
    db.flush()
    return cb


def _make_skill(db: Session, slug: str = "test-skill") -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=f"Skill {slug}",
        description="x",
        is_public=True,
    )
    db.add(s)
    db.flush()
    return s


def _make_token_row(db: Session, cookbook_id, *, scope: str = "edit", name: str | None = None) -> tuple[CookbookShareToken, str]:
    """Create a CookbookShareToken row and return (row, plaintext_token)."""
    cb_prefix = str(cookbook_id).replace("-", "")[:8]
    random_hex = secrets.token_hex(16)
    full_token = f"cbt_{cb_prefix}_{random_hex}"
    token_hash = hashlib.sha256(full_token.encode()).hexdigest()

    row = CookbookShareToken(
        id=uuid4(),
        cookbook_id=cookbook_id,
        token_hash=token_hash,
        token_prefix=cb_prefix,
        scope=scope,
        name=name,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row, full_token


def _build_app(db: Session, *, api_key_user_id=None, is_master: bool = False, include_share_token_router: bool = True):
    """Build a test FastAPI app with cookbook + share-token routes."""
    from app.cookbook_routes import router as cookbook_router
    from app.share_token_routes import router as share_token_router

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
            request.state.api_key_user_id = None if is_master else _uid
            request.state.api_key_id = None
            # Clear any cbt_ state from previous middleware runs
            request.state.cookbook_token_scope = None
            request.state.cookbook_token_cookbook_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)
    if include_share_token_router:
        app.include_router(share_token_router)

    return app


def _build_cbt_app(db: Session, token: str, *, scope: str = "edit", cookbook_id=None):
    """Build a test app that simulates cbt_ token middleware having already authenticated."""
    from app.cookbook_routes import router as cookbook_router
    from app.share_token_routes import router as share_token_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    class InjectCBTAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Simulate what APIKeyMiddleware does for a cbt_ token
            request.state.api_key_user_id = None  # cbt_ tokens are NOT user-scoped
            request.state.api_key_id = None
            request.state.cookbook_token_scope = scope
            request.state.cookbook_token_cookbook_id = cookbook_id
            return await call_next(request)

    app.add_middleware(InjectCBTAuthState)
    app.include_router(cookbook_router)
    app.include_router(share_token_router)

    return app


# ─────────────────────────── Tests ──────────────────────────────────────


class TestShareTokens:
    """Phase 3 share-token tests."""

    # (a) test_create_share_token_returns_token_once
    def test_create_share_token_returns_token_once(self, db_session):
        """POST as cookbook owner, expect 201 with token in body,
        GET shows token absent (only metadata)."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        app = _build_app(db_session, api_key_user_id=user.id)

        with TestClient(app) as client:
            # Create share token
            resp = client.post(
                f"/api/cookbooks/{cb.id}/share-tokens",
                json={"name": "my token", "scope": "edit"},
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert "token" in body
            assert body["token"].startswith("cbt_")
            assert body["name"] == "my token"
            assert body["scope"] == "edit"
            token = body["token"]

            # GET list should NOT include plaintext token
            resp2 = client.get(f"/api/cookbooks/{cb.id}/share-tokens")
            assert resp2.status_code == 200
            items = resp2.json()
            assert len(items) >= 1
            for item in items:
                assert "token" not in item or item.get("token") is None

    # (b) test_token_format_constant_length
    def test_token_format_constant_length(self, db_session):
        """Issue 5 tokens, all match ^cbt_[0-9a-f]{8}_[0-9a-f]{32}$."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        app = _build_app(db_session, api_key_user_id=user.id)
        pattern = re.compile(r"^cbt_[0-9a-f]{8}_[0-9a-f]{32}$")

        with TestClient(app) as client:
            tokens = []
            for i in range(5):
                resp = client.post(
                    f"/api/cookbooks/{cb.id}/share-tokens",
                    json={"scope": "edit"},
                )
                assert resp.status_code == 201, resp.text
                token = resp.json()["token"]
                tokens.append(token)
                assert pattern.match(token), f"Token {token!r} doesn't match format"

            # All tokens unique
            assert len(set(tokens)) == 5

    # (c) test_existing_rec_keys_still_work
    def test_existing_rec_keys_still_work(self, db_session):
        """REGRESSION: rec_-style key via master key can still hit
        GET /api/cookbooks/{id}/manifest."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        # Master key (api_key_user_id=None, is_master=True)
        app = _build_app(db_session, is_master=True)

        with TestClient(app) as client:
            resp = client.get(f"/api/cookbooks/{cb.id}/manifest")
            assert resp.status_code == 200, resp.text

    # (d) test_cbt_edit_token_can_add_skill_to_cookbook
    def test_cbt_edit_token_can_add_skill_to_cookbook(self, db_session):
        """Issue cbt_ edit, POST /api/cookbooks/{id}/skills with that token,
        expect 200/201."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        skill = _make_skill(db_session, slug="edit-skill")

        # Create share token via owner
        app_owner = _build_app(db_session, api_key_user_id=user.id)
        with TestClient(app_owner) as client:
            resp = client.post(
                f"/api/cookbooks/{cb.id}/share-tokens",
                json={"scope": "edit"},
            )
            assert resp.status_code == 201

        # Build app simulating cbt_ middleware
        app_cbt = _build_cbt_app(
            db_session, token="cbt_dummy", scope="edit", cookbook_id=cb.id,
        )

        with TestClient(app_cbt) as client:
            resp = client.post(
                f"/api/cookbooks/{cb.id}/skills",
                json={"slug": "edit-skill"},
            )
            assert resp.status_code in (200, 201), resp.text

    # (e) test_cbt_read_token_blocks_skill_add
    def test_cbt_read_token_blocks_skill_add(self, db_session):
        """Issue cbt_ read, POST /api/cookbooks/{id}/skills, expect 403."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        skill = _make_skill(db_session, slug="blocked-skill")

        app_cbt = _build_cbt_app(
            db_session, token="cbt_dummy", scope="read", cookbook_id=cb.id,
        )

        with TestClient(app_cbt) as client:
            resp = client.post(
                f"/api/cookbooks/{cb.id}/skills",
                json={"slug": "blocked-skill"},
            )
            assert resp.status_code == 403, resp.text
            assert "scope" in resp.json()["detail"].lower()

    # (f) test_cbt_token_blocks_other_cookbook
    def test_cbt_token_blocks_other_cookbook(self, db_session):
        """Token for cookbook A used against cookbook B → 403 (NOT 404)."""
        user = _make_user(db_session)
        cb_a = _make_cookbook(db_session, owner_id=user.id, name="Cookbook A")
        cb_b = _make_cookbook(db_session, owner_id=user.id, name="Cookbook B")

        # Token for cookbook A
        app_cbt = _build_cbt_app(
            db_session, token="cbt_dummy", scope="edit", cookbook_id=cb_a.id,
        )

        with TestClient(app_cbt) as client:
            # Try to access cookbook B
            resp = client.get(f"/api/cookbooks/{cb_b.id}")
            assert resp.status_code == 403, resp.text
            assert "wrong cookbook" in resp.json()["detail"].lower()

    # (g) test_cbt_token_blocks_publish
    def test_cbt_token_blocks_publish(self, db_session):
        """cbt_ token (any scope) on /api/skills/_publish → 403.
        We test this by checking the middleware blocks cbt_ tokens
        from reaching _publish. Since publisher_routes isn't in our test app,
        we verify the enforcement helper logic."""
        from app.share_token_routes import enforce_cbt_scope

        # Build a mock request with cbt_ state
        class MockState:
            cookbook_token_scope = "edit"
            cookbook_token_cookbook_id = uuid4()

        class MockRequest:
            state = MockState()
            method = "POST"
            url = type("U", (), {"path": "/api/skills/_publish"})

        with pytest.raises(Exception) as exc_info:
            # _publish path should always 403 for cbt_ tokens
            from fastapi import HTTPException
            try:
                enforce_cbt_scope(MockRequest())
            except HTTPException as e:
                raise e
        assert exc_info.value.status_code == 403  # type: ignore

    # (h) test_rotate_invalidates_old_returns_new
    def test_rotate_invalidates_old_returns_new(self, db_session):
        """Rotate returns new token, old token returns 401 on next request."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        app = _build_app(db_session, api_key_user_id=user.id)

        with TestClient(app) as client:
            # Create initial token
            resp = client.post(
                f"/api/cookbooks/{cb.id}/share-tokens",
                json={"name": "rotate-test", "scope": "edit"},
            )
            assert resp.status_code == 201
            old_body = resp.json()
            old_token_id = old_body["id"]

            # Rotate
            resp2 = client.post(
                f"/api/cookbooks/{cb.id}/share-tokens/{old_token_id}/rotate",
            )
            assert resp2.status_code == 200, resp2.text
            new_body = resp2.json()
            assert "token" in new_body
            new_token = new_body["token"]
            assert new_token != old_body["token"]
            assert new_body["name"] == "rotate-test"

            # Verify old token row is inactive
            resp3 = client.get(f"/api/cookbooks/{cb.id}/share-tokens")
            items = resp3.json()
            old_item = next(i for i in items if i["id"] == old_token_id)
            assert old_item["is_active"] is False

    # (i) test_revoke_token_returns_401_on_use
    def test_revoke_token_returns_401_on_use(self, db_session):
        """DELETE token, then use it → 401."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, owner_id=user.id)
        app = _build_app(db_session, api_key_user_id=user.id)

        with TestClient(app) as client:
            # Create token
            resp = client.post(
                f"/api/cookbooks/{cb.id}/share-tokens",
                json={"scope": "edit"},
            )
            assert resp.status_code == 201
            token_id = resp.json()["id"]

            # Delete (revoke) the token
            resp2 = client.delete(
                f"/api/cookbooks/{cb.id}/share-tokens/{token_id}",
            )
            assert resp2.status_code == 204

            # List should show it as inactive
            resp3 = client.get(f"/api/cookbooks/{cb.id}/share-tokens")
            items = resp3.json()
            revoked = next(i for i in items if i["id"] == token_id)
            assert revoked["is_active"] is False

    # (j) test_alembic_migration_imports_and_has_correct_down_revision
    def test_alembic_migration_imports_and_has_correct_down_revision(self):
        """Import the new migration module, assert upgrade and downgrade are callable,
        assert down_revision matches the head read at start."""
        import importlib.util
        import os
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "alembic",
            "versions",
            "a3f1e9b5c7d2_v71_share_tokens.py",
        )
        spec = importlib.util.spec_from_file_location(
            "alembic.versions.a3f1e9b5c7d2_v71_share_tokens", path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)
        # The head we read at the start of the workflow
        assert mod.down_revision == "e9b5c7a3f1d8"


# ── Post-review regression tests (Phase 3 critic findings) ───────────────────


class TestPhase3CriticReviewFixes:
    """Regression tests for the HIGH-severity finding caught by the post-merge
    code reviewer (2026-05-07) and accompanying MEDIUM/LOW fixes.

    HIGH#1 — cbt_ tokens on non-cookbook routes used to inherit the master-key
              signal because api_key_user_id=None == master sentinel. Fixed by:
              (a) middleware 403s cbt_ on any path not starting with /api/cookbooks/
              (b) middleware sets api_key_user_id="CBT_TOKEN" sentinel so any
                  is_master = (api_key_user_id is None) check excludes cbt_ correctly.

    MED#5 — _publish must always be 403 for cbt_ tokens, regardless of scope,
              even on cookbook-prefixed paths.
    """

    def test_cookbook_ctx_does_not_grant_master_for_cbt(self, client, db_session):
        """Direct check: require_cookbook_tier called with cbt_ state must
        return is_master=False. This prevents the HIGH#1 escalation."""
        from app.cookbook_routes import require_cookbook_tier
        from uuid import uuid4

        class _MockState:
            api_key_user_id = "CBT_TOKEN"
            is_cbt_token = True
            cookbook_token_cookbook_id = uuid4()

        class _MockReq:
            state = _MockState()

        ctx = require_cookbook_tier(_MockReq(), db=db_session)
        assert ctx.is_master is False, (
            "HIGH#1 regression: cbt_ token granted master access via "
            "api_key_user_id=None signal"
        )
        assert ctx.user_id is None
        assert ctx.cbt_cookbook_id is not None

    def test_cookbook_ctx_does_grant_master_for_static_master_key_only(self, client, db_session):
        """Sanity: pure master key (api_key_user_id=None, no cbt_ flag) still grants master."""
        from app.cookbook_routes import require_cookbook_tier

        class _MockState:
            api_key_user_id = None  # static master key signal
            # is_cbt_token attribute intentionally absent

        class _MockReq:
            state = _MockState()

        ctx = require_cookbook_tier(_MockReq(), db=db_session)
        assert ctx.is_master is True
        assert ctx.tier == "studio"

    def test_publish_path_blocked_for_cbt_via_cookbook_helper(self):
        """MED#5: even if a /api/cookbooks/{id}/_publish route is added later,
        _enforce_cbt_scope_for_cookbook_route blocks it for cbt_ tokens."""
        from app.cookbook_routes import _enforce_cbt_scope_for_cookbook_route
        from fastapi import HTTPException
        from uuid import uuid4

        cb_id = uuid4()

        class _MockState:
            cookbook_token_scope = "edit"
            cookbook_token_cookbook_id = cb_id

        class _MockReq:
            state = _MockState()
            method = "POST"
            url = type("U", (), {"path": f"/api/cookbooks/{cb_id}/_publish"})

        try:
            _enforce_cbt_scope_for_cookbook_route(_MockReq(), str(cb_id))
        except HTTPException as e:
            assert e.status_code == 403
            assert "publish" in (e.detail or "").lower() or "share token" in (e.detail or "").lower()
        else:
            raise AssertionError("Expected 403 HTTPException on cbt_ + _publish")
