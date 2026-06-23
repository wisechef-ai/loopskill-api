"""repohygiene_2605 Phase H.1 — cbt_token can install public-catalog skills.

Issue #290: pro cookbook share-tokens should be able to install entitled
public-catalog skills directly via GET /api/skills/install.

Auth-design choice: Option C — new ``allow_public_catalog`` boolean column on
CookbookShareToken, default True for pro/pro_plus owners.  Parallels the
existing scope ladder without reusing the 8-char scope string (which has no
room for "install:public").

Test matrix:
  test 1: pro cbt_token + public skill → 200 + tarball_url
  test 2: non-pro (free) cbt_token + public skill → 403 (regression guard)
  test 3: pro cbt_token + PRIVATE skill not in cookbook → 404 (no leak)
  test 4: pro cbt_token → /api/admin/* → 403 (middleware path restriction)
"""
from __future__ import annotations

import hashlib
import secrets
import tarfile
import tempfile
from pathlib import Path
from typing import Generator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
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


# ─────────────────────────── Fixtures ────────────────────────────────────


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


# ─────────────────────────── Helpers ─────────────────────────────────────


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


def _make_cookbook(db: Session, owner_id: UUID, name: str = "CB") -> Cookbook:
    cb = Cookbook(
        id=uuid4(),
        name=name,
        description="test",
        is_base=False,
        bundle_owner=owner_id,
    )
    db.add(cb)
    db.flush()
    return cb


def _make_skill(
    db: Session,
    slug: str,
    is_public: bool = True,
    tier: str = "free",
) -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=f"Skill {slug}",
        description="x",
        is_public=is_public,
        tier=tier,
    )
    db.add(s)
    db.flush()
    return s


def _make_skill_version(db: Session, skill: Skill, semver: str = "1.0.0") -> SkillVersion:
    # Create a minimal real tarball so the _download route doesn't 404 on disk.
    tmp = tempfile.mktemp(suffix=".tar.gz")  # noqa: S306 — test-only tmpfile
    with tarfile.open(tmp, "w:gz"):
        pass  # empty tarball is fine for install URL tests
    sv = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver=semver,
        checksum_sha256="a" * 64,
        tarball_path=tmp,
    )
    db.add(sv)
    db.flush()
    return sv


def _make_cbt_token(
    db: Session,
    cookbook_id: UUID,
    scope: str = "install",
    allow_public_catalog: bool = True,
) -> tuple[CookbookShareToken, str]:
    """Create a CookbookShareToken row and return (row, plaintext_token)."""
    cb_prefix = str(cookbook_id).replace("-", "")[:8]
    random_hex = secrets.token_hex(16)
    full_token = f"cbt_{cb_prefix}_{random_hex}"
    token_hash = hashlib.sha256(full_token.encode()).hexdigest()
    row = CookbookShareToken(
        id=uuid4(),
        bundle_id=cookbook_id,
        token_hash=token_hash,
        token_prefix=cb_prefix,
        scope=scope,
        is_active=True,
        allow_public_catalog=allow_public_catalog,
    )
    db.add(row)
    db.flush()
    return row, full_token


def _build_install_app(db: Session, cbt_auth_ctx: AuthContext) -> FastAPI:
    """Test app simulating APIKeyMiddleware after cbt_ token auth on /api/skills/install.

    Unlike the full middleware, we inject a pre-resolved AuthContext so tests
    don't need a live Redis / DB connection-pool. This mirrors the pattern used
    in test_cookbook_share_install.py's _build_cbt_app helper.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.install_routes import router as install_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    class InjectCBTAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Mirror what middleware.py stamps for a cbt_ token with allow_public_catalog
            request.state.api_key_user_id = "CBT_TOKEN"
            request.state.api_key_id = None
            request.state.is_cbt_token = True
            request.state.is_anonymous_free_install = False
            request.state.auth_ctx = cbt_auth_ctx
            return await call_next(request)

    app.add_middleware(InjectCBTAuthState)
    app.include_router(install_router, prefix="/api")
    return app


# ─────────────────────────── Test 1 ──────────────────────────────────────


class TestProCbtTokenPublicSkillInstall:
    """test 1: pro cbt_token + public skill → 200 + tarball_url."""

    def test_pro_cbt_token_can_install_public_catalog_skill(self, db_session):
        """GET /api/skills/install?slug=<public>&mode=files with pro cbt_token returns 200."""
        owner = _make_user(db_session, tier="pro")
        cb = _make_cookbook(db_session, owner.id)
        # Public skill NOT in the cookbook (the whole point of issue #290)
        public_skill = _make_skill(db_session, slug="ruthless-mentor-h1", is_public=True, tier="free")
        _make_skill_version(db_session, public_skill)
        _make_cbt_token(db_session, cb.id, scope="install", allow_public_catalog=True)
        db_session.commit()

        # Build app with pro cbt_token AuthContext (allow_public_catalog=True)
        cbt_ctx = AuthContext(
            scope="cbt_token",
            cookbook_scope=cb.id,
            allow_public_catalog=True,
        )
        app = _build_install_app(db_session, cbt_ctx)
        client = TestClient(app)

        resp = client.get("/api/skills/install", params={"slug": "ruthless-mentor-h1", "mode": "files"})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "tarball_url" in body, f"Missing tarball_url in: {body}"
        assert "/api/skills/_download" in body["tarball_url"]


# ─────────────────────────── Test 2 ──────────────────────────────────────


class TestNonProCbtTokenPublicSkillInstall:
    """test 2: cbt_token with allow_public_catalog=False → 403 (regression guard)."""

    def test_non_pro_cbt_token_cannot_install_public_skill(self, db_session):
        """Non-pro (allow_public_catalog=False) cbt_token still gets 403."""
        owner = _make_user(db_session, tier="free")
        cb = _make_cookbook(db_session, owner.id)
        public_skill = _make_skill(db_session, slug="public-skill-nonpro-h1", is_public=True, tier="free")
        _make_skill_version(db_session, public_skill)
        _make_cbt_token(db_session, cb.id, scope="install", allow_public_catalog=False)
        db_session.commit()

        # Non-pro token: allow_public_catalog=False
        cbt_ctx = AuthContext(
            scope="cbt_token",
            cookbook_scope=cb.id,
            allow_public_catalog=False,
        )
        app = _build_install_app(db_session, cbt_ctx)
        client = TestClient(app)

        resp = client.get("/api/skills/install", params={"slug": "public-skill-nonpro-h1", "mode": "files"})
        # With allow_public_catalog=False, the install_route must reject the cbt_token caller
        assert resp.status_code == 403, (
            f"Expected 403 for non-pro cbt_token, got {resp.status_code}: {resp.text}"
        )


# ─────────────────────────── Test 3 ──────────────────────────────────────


class TestProCbtTokenPrivateSkillNotInCookbook:
    """test 3: pro cbt_token + PRIVATE skill not in cookbook → 404 (no leak)."""

    def test_pro_cbt_token_cannot_install_private_skill_not_in_cookbook(self, db_session):
        """allow_public_catalog widens ONLY public skills — private skills stay gated."""
        owner = _make_user(db_session, tier="pro")
        cb = _make_cookbook(db_session, owner.id)
        # Private skill, NOT in the cookbook
        private_skill = _make_skill(
            db_session, slug="private-skill-h1", is_public=False, tier="pro"
        )
        _make_skill_version(db_session, private_skill)
        db_session.commit()

        cbt_ctx = AuthContext(
            scope="cbt_token",
            cookbook_scope=cb.id,
            allow_public_catalog=True,
        )
        app = _build_install_app(db_session, cbt_ctx)
        client = TestClient(app)

        resp = client.get("/api/skills/install", params={"slug": "private-skill-h1", "mode": "files"})
        # Private skill + cbt_token (not the skill's owner) → 404 (no existence leak)
        assert resp.status_code == 404, (
            f"Expected 404 for private skill, got {resp.status_code}: {resp.text}"
        )


# ─────────────────────────── Test 4 ──────────────────────────────────────


class TestMiddlewarePathRestrictionNotRegressed:
    """test 4: cbt_token → /api/admin/* → 403 (middleware path gate must not regress)."""

    def test_cbt_token_blocked_from_admin_routes(self):
        """The middleware path restriction for cbt_ tokens (non-cookbook routes) still holds.

        We spin up the REAL middleware stack and send a raw cbt_ token header to
        an /api/admin/* path. The middleware must return 403 with the canonical
        'Share tokens can only access cookbook routes' message.

        Because the real middleware does a DB lookup for the cbt_ token, we create
        a minimal self-contained app with a fixed cbt-shaped key that will NOT match
        any DB row (so the 401 «invalid token» path would fire IF the middleware got
        that far). The gate we are pinning fires BEFORE the DB lookup — it blocks
        /api/admin/* unconditionally for any cbt_-prefixed key.
        """
        from app.main import create_app

        # Use the full app (has APIKeyMiddleware) against a test-only SQLite DB.
        from sqlalchemy import create_engine as _ce
        from sqlalchemy.orm import sessionmaker as _sm

        _engine = _ce("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=_engine)
        _Session = _sm(bind=_engine, autocommit=False, autoflush=False)

        def _override_db():
            s = _Session()
            try:
                yield s
            finally:
                s.close()

        app = create_app()
        app.dependency_overrides[get_db] = _override_db
        client = TestClient(app, raise_server_exceptions=False)

        # Any cbt_-prefixed key — format must be valid (cbt_<8hex>_<32hex>)
        # so the middleware reaches the path-restriction check first.
        fake_cbt_key = "cbt_" + "a" * 8 + "_" + "b" * 32

        resp = client.get(
            "/api/admin/reindex-all",
            headers={"x-api-key": fake_cbt_key},
        )
        # The middleware fires 403 BEFORE the DB token lookup for non-cookbook paths.
        assert resp.status_code == 403, (
            f"Expected 403 for cbt_ key on /api/admin/*, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "share token" in body.get("detail", "").lower(), (
            f"Expected canonical share-token error message, got: {body}"
        )


# ─────────────────────────── authz unit tests ────────────────────────────


class TestAuthzCanInstallPublicCatalogClause:
    """Unit tests for the allow_public_catalog path in authz.can_read_skill."""

    def test_public_skill_is_always_readable_by_anyone(self, db_session):
        """Public skill returns True before the cbt_token clause (existing behaviour)."""
        from app.authz import can_read_skill

        public_skill = _make_skill(db_session, slug="pub-unit", is_public=True)
        db_session.flush()

        # Even an anonymous-ish cbt_token can read a public skill (is_public short-circuits)
        ctx = AuthContext(scope="cbt_token", cookbook_scope=uuid4(), allow_public_catalog=True)
        assert can_read_skill(ctx, public_skill, db=db_session) is True

    def test_private_skill_not_in_cookbook_still_returns_false(self, db_session):
        """Private skill + cbt_token (not in cookbook) → False, regardless of allow_public_catalog."""
        from app.authz import can_read_skill

        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner.id)
        private_skill = _make_skill(db_session, slug="priv-unit", is_public=False)
        db_session.flush()

        ctx = AuthContext(scope="cbt_token", cookbook_scope=cb.id, allow_public_catalog=True)
        # Private, not in cookbook → False
        assert can_read_skill(ctx, private_skill, db=db_session) is False
