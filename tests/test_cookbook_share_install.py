"""cookbook_share_2105 — contract pin for share-token install path.

These tests fail against the current codebase. They MUST all pass before the
sprint closes. Each test classes pins one of the four bug-classes in the
plan-doc matrix plus the new MCP tool + Alembic migration.

Run:
    pytest -q tests/test_cookbook_share_install.py
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Generator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth_ctx import AuthContext
from app.authz import can_install, can_read_skill
from app.database import get_db
from app.models import (
    Base,
    Cookbook,
    CookbookShareToken,
    CookbookSkill,
    Skill,
    SkillVersion,
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


def _make_user(db: Session, tier: str = "pro", status: str = "active") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Owner",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(user)
    db.flush()
    return user


def _make_cookbook(db: Session, owner_id: UUID, name: str = "Cookbook") -> Cookbook:
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


def _make_skill(
    db: Session, slug: str = "ahe-skill", is_public: bool = False, owner_id: UUID | None = None
) -> Skill:
    # Skill has no skill_owner column — authz.can_read_skill reads it via
    # getattr(skill, "skill_owner", None), so we don't pass it. owner_id is
    # accepted for test readability but not stored on the model.
    _ = owner_id
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=f"Skill {slug}",
        description="x",
        is_public=is_public,
    )
    db.add(s)
    db.flush()
    return s


def _add_skill_to_cookbook(db: Session, cookbook: Cookbook, skill: Skill, source: str = "custom-added") -> CookbookSkill:
    cs = CookbookSkill(cookbook_id=cookbook.id, skill_id=skill.id, source=source)
    db.add(cs)
    db.flush()
    return cs


def _make_skill_version(db: Session, skill: Skill, semver: str = "1.0.0") -> SkillVersion:
    sv = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver=semver,
        checksum_sha256="a" * 64,
        tarball_path=f"/tmp/{skill.slug}-{semver}.tar.gz",
    )
    db.add(sv)
    db.flush()
    return sv


def _make_token_row(
    db: Session, cookbook_id: UUID, scope: str = "edit"
) -> tuple[CookbookShareToken, str]:
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
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row, full_token


def _build_cbt_app(db: Session, scope: str, cookbook_id: UUID):
    """Test app simulating APIKeyMiddleware after cbt_ token authentication."""
    from app.cookbook_routes import router as cookbook_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    class InjectCBTAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Match what app/middleware.py:444-451 sets for a cbt_ token
            request.state.api_key_user_id = "CBT_TOKEN"
            request.state.api_key_id = None
            request.state.is_cbt_token = True
            request.state.cookbook_token_scope = scope
            request.state.cookbook_token_cookbook_id = cookbook_id
            request.state.auth_ctx = AuthContext(
                scope="cbt_token",
                cookbook_scope=cookbook_id,
            )
            return await call_next(request)

    app.add_middleware(InjectCBTAuthState)
    app.include_router(cookbook_router)
    return app


# ─────────────────────────── 1. predicate ──────────────────────────────


class TestCanReadSkillForCookbookScope:
    """authz.can_read_skill must honour cookbook-scope tokens."""

    def test_cbt_token_can_read_skill_in_scoped_cookbook(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        ctx = AuthContext(scope="cbt_token", cookbook_scope=cb.id)

        # New 4-arg signature: db threaded through for the cookbook-skill lookup
        assert can_read_skill(ctx, skill, db=db_session) is True

    def test_cbt_token_cannot_read_skill_not_in_scoped_cookbook(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        # skill exists but is NOT in the cookbook
        skill = _make_skill(db_session, is_public=False, owner_id=owner.id)
        ctx = AuthContext(scope="cbt_token", cookbook_scope=cb.id)

        assert can_read_skill(ctx, skill, db=db_session) is False
        assert can_install(ctx, skill, db=db_session) is False

    def test_cbt_token_can_still_read_public_skills(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        public_skill = _make_skill(db_session, slug="public-skill", is_public=True)
        ctx = AuthContext(scope="cbt_token", cookbook_scope=cb.id)

        # Public-skill clause runs first, no DB lookup needed
        assert can_read_skill(ctx, public_skill, db=db_session) is True


# ─────────────────────────── 2. manifest ───────────────────────────────


class TestCookbookManifestWithCbtToken:
    def test_cbt_can_get_manifest_of_scoped_cookbook(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id, name="Shared Cookbook")
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        _make_token_row(db_session, cb.id, scope="edit")
        db_session.commit()

        app = _build_cbt_app(db_session, scope="edit", cookbook_id=cb.id)
        client = TestClient(app)

        resp = client.get(f"/api/cookbooks/{cb.id}/manifest")
        # CURRENTLY: 404 cookbook_not_found because _resolve_owned_cookbook
        # ignores cbt_cookbook_id. After fix: 200 with skills listed.
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "ahe" in body
        assert cb.name in body

    def test_cbt_cannot_get_manifest_of_other_cookbook(self, db_session):
        owner = _make_user(db_session)
        cb_a = _make_cookbook(db_session, owner.id, name="A")
        cb_b = _make_cookbook(db_session, owner.id, name="B")
        db_session.commit()

        # Token scoped to A, requesting B → 403 wrong cookbook (existing gate)
        app = _build_cbt_app(db_session, scope="edit", cookbook_id=cb_a.id)
        client = TestClient(app)
        resp = client.get(f"/api/cookbooks/{cb_b.id}/manifest")
        assert resp.status_code == 403
        assert "wrong cookbook" in resp.text.lower()


# ─────────────────────────── 3. bulk install ───────────────────────────


class TestCookbookBulkInstallWithCbtToken:
    def test_install_scope_succeeds_returns_signed_urls(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        _make_skill_version(db_session, skill)
        _make_token_row(db_session, cb.id, scope="install")
        db_session.commit()

        app = _build_cbt_app(db_session, scope="install", cookbook_id=cb.id)
        client = TestClient(app)

        resp = client.post(f"/api/cookbooks/{cb.id}/install")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cookbook_id"] == str(cb.id)
        assert len(body["skills"]) == 1
        assert body["skills"][0]["slug"] == "ahe"
        assert body["skills"][0]["tarball_url"] is not None
        assert "/api/skills/_download?token=" in body["skills"][0]["tarball_url"]

    def test_read_scope_returns_403_with_scope_insufficient(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        _make_skill_version(db_session, skill)
        db_session.commit()

        app = _build_cbt_app(db_session, scope="read", cookbook_id=cb.id)
        client = TestClient(app)
        resp = client.post(f"/api/cookbooks/{cb.id}/install")
        assert resp.status_code == 403
        # New, clearer error code from Phase D
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        # Accept either the structured payload or a detail string mentioning insufficient scope.
        text = resp.text.lower()
        assert "insufficient" in text or "scope_insufficient" in text

    def test_empty_cookbook_returns_200_with_empty_skills(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id, name="Empty")
        _make_token_row(db_session, cb.id, scope="install")
        db_session.commit()

        app = _build_cbt_app(db_session, scope="install", cookbook_id=cb.id)
        client = TestClient(app)
        resp = client.post(f"/api/cookbooks/{cb.id}/install")
        assert resp.status_code == 200, resp.text
        assert resp.json()["skills"] == []


# ─────────────────────────── 4. single-skill install ───────────────────


class TestSingleSkillInstallUnderCookbookPrefix:
    """GET /api/cookbooks/{cookbook_id}/skills/{slug}/install (NEW route, Phase D)."""

    def test_single_skill_install_with_cbt_returns_signed_url(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        _make_skill_version(db_session, skill, "1.0.1")
        _make_token_row(db_session, cb.id, scope="install")
        db_session.commit()

        app = _build_cbt_app(db_session, scope="install", cookbook_id=cb.id)
        client = TestClient(app)
        resp = client.get(f"/api/cookbooks/{cb.id}/skills/ahe/install")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["slug"] == "ahe"
        assert body["version"] == "1.0.1"
        assert "/api/skills/_download?token=" in body["tarball_url"]

    def test_single_skill_install_slug_not_in_cookbook_returns_404(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        # Skill exists globally but NOT in this cookbook
        skill = _make_skill(db_session, slug="other", is_public=False, owner_id=owner.id)
        _make_skill_version(db_session, skill, "1.0.0")
        _make_token_row(db_session, cb.id, scope="install")
        db_session.commit()

        app = _build_cbt_app(db_session, scope="install", cookbook_id=cb.id)
        client = TestClient(app)
        resp = client.get(f"/api/cookbooks/{cb.id}/skills/other/install")
        assert resp.status_code == 404

    def test_single_skill_install_read_scope_returns_403(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        _make_skill_version(db_session, skill)
        db_session.commit()

        app = _build_cbt_app(db_session, scope="read", cookbook_id=cb.id)
        client = TestClient(app)
        resp = client.get(f"/api/cookbooks/{cb.id}/skills/ahe/install")
        # GET install with read-only scope is a behaviour question:
        # install IS a write-flavoured action even though it's GET.
        # Phase D decision: read-scope is rejected on install routes specifically.
        assert resp.status_code == 403


# ─────────────────────────── 5. MCP tool ────────────────────────────────


class TestMcpCookbookInstall:
    """recipes_cookbook_install MCP tool — NEW in Phase F."""

    def test_mcp_tool_bulk_install_with_cbt_token_default_cookbook_id(self, db_session):
        from app.mcp.tools.cookbook_install import recipes_cookbook_install

        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        _make_skill_version(db_session, skill)
        db_session.commit()

        ctx = AuthContext(scope="cbt_token", cookbook_scope=cb.id)
        result = recipes_cookbook_install(ctx=ctx, db=db_session)
        assert result["cookbook_id"] == str(cb.id)
        assert len(result["skills"]) == 1
        assert result["skills"][0]["slug"] == "ahe"

    def test_mcp_tool_single_skill_via_slug_arg(self, db_session):
        from app.mcp.tools.cookbook_install import recipes_cookbook_install

        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill_a = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        skill_b = _make_skill(db_session, slug="other", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill_a)
        _add_skill_to_cookbook(db_session, cb, skill_b)
        _make_skill_version(db_session, skill_a)
        _make_skill_version(db_session, skill_b)
        db_session.commit()

        ctx = AuthContext(scope="cbt_token", cookbook_scope=cb.id)
        result = recipes_cookbook_install(ctx=ctx, db=db_session, slug="ahe")
        # Single-skill shape mirrors /api/skills/install: slug + version + tarball_url
        assert result["slug"] == "ahe"
        assert "/api/skills/_download?token=" in result["tarball_url"]

    def test_mcp_tool_master_key_requires_explicit_cookbook_id(self, db_session):
        from app.mcp.tools.cookbook_install import recipes_cookbook_install

        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        _make_skill_version(db_session, skill)
        db_session.commit()

        master_ctx = AuthContext(scope="master")
        # Without cookbook_id arg: master cannot infer scope → 422-style error
        with pytest.raises((ValueError, KeyError, TypeError, Exception)):
            recipes_cookbook_install(ctx=master_ctx, db=db_session)

        # With explicit cookbook_id: works
        result = recipes_cookbook_install(ctx=master_ctx, db=db_session, cookbook_id=str(cb.id))
        assert result["cookbook_id"] == str(cb.id)


# ─────────────────────────── 6. scope migration ────────────────────────


class TestShareTokenScopeMigration:
    """Phase E — install scope value added, existing tokens auto-upgraded."""

    def test_install_is_valid_scope_value(self, db_session):
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        db_session.commit()

        # Should not raise on flush — Phase E relaxes the CHECK constraint
        row, _ = _make_token_row(db_session, cb.id, scope="install")
        db_session.commit()
        assert row.scope == "install"

    def test_default_share_token_create_uses_install_scope(self, db_session):
        """recipes_share_create / share_token_routes default scope flips to install."""
        from app.share_token_routes import _create_share_token_service

        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        skill = _make_skill(db_session, slug="ahe", is_public=False, owner_id=owner.id)
        _add_skill_to_cookbook(db_session, cb, skill)
        db_session.commit()

        # No scope arg = default
        ctx_user = AuthContext(scope="user", user_id=owner.id)
        result = _create_share_token_service(
            db=db_session,
            cookbook_id=str(cb.id),
            name=None,
            ctx=ctx_user,
            # NOTE: scope omitted on purpose — must default to "install" after Phase E.
        )
        assert result["scope"] == "install"
