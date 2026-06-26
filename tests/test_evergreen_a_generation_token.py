"""evergreen_0206 Phase A — generation-token truthfulness.

THE VERIFIED GAP (recon 2026-06-02): SQLAlchemy `onupdate=func.now()` on
`Cookbook.updated_at` fires only when the *Cookbook* row itself is UPDATEd. It
does NOT fire when a child `CookbookSkill` row is added, removed, or has its
`pinned_version` rewritten. So `Cookbook.updated_at` — which we want to use as
the cheap-poll *generation token* (If-None-Match → 304) — lied: a cookbook's
skill set could change while its generation stayed frozen, and a subscribed
agent would never reconcile.

This suite pins the contract: ANY mutation to a cookbook's declared skill set
advances the parent generation token. Three write paths:
  1. add_skill_to_cookbook        (bundle_routes.py)
  2. remove_skill_from_cookbook   (bundle_routes.py)
  3. recipes_sync pin-write       (mcp/tools/recipes_sync.py)

RED until Phase A wires `_touch_cookbook_generation(db, cb)` into all three.
"""

from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import get_db
from app.models import Base, Bundle, BundleSkill, Skill, SkillVersion, User


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


def _make_user(db: Session, *, tier: str = "pro") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Gen Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _make_cookbook(db: Session, owner: User) -> Bundle:
    cb = Bundle(id=uuid4(), name="Gen CB", is_base=False, bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    return cb


def _make_skill(db: Session, slug: str, *, with_version: bool = True) -> Skill:
    s = Skill(id=uuid4(), slug=slug, title=f"Skill {slug}", description="x", is_public=True)
    db.add(s)
    db.flush()
    if with_version:
        db.add(
            SkillVersion(
                id=uuid4(),
                skill_id=s.id,
                semver="0.1.0",
                tarball_path=f"/tmp/{slug}.tar.gz",
                tarball_size_bytes=42,
                checksum_sha256="a" * 64,
            )
        )
        db.flush()
    return s


def _make_app(db: Session, owner_id) -> FastAPI:
    from app.bundle_routes import router as cookbook_router

    app = FastAPI()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = owner_id
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)
    return app


def _generation(db: Session, cb_id) -> object:
    """Read the live generation token (Cookbook.updated_at) fresh from DB."""
    db.expire_all()
    cb = db.query(Bundle).filter(Bundle.id == cb_id).first()
    return cb.updated_at


def _backdate(db: Session, cb_id) -> object:
    """Pin the cookbook's generation token to a fixed past instant and commit.

    SQLite's ``func.now()`` resolves to whole seconds, so a sub-second test
    delay can't reliably prove a bump via ``after > before``. Backdating to a
    fixed 2020 timestamp makes any real ``func.now()`` touch unambiguously
    greater, regardless of clock granularity — testing the contract (the write
    happened) rather than the wall clock. Returns the backdated value.
    """
    from datetime import datetime, timezone

    old = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    db.query(Bundle).filter(Bundle.id == cb_id).update({"updated_at": old}, synchronize_session=False)
    db.commit()
    return _generation(db, cb_id)


# ─────────────────────── Generation-token contract ──────────────────────


class TestGenerationTokenAdvancesOnChildMutation:
    """Cookbook.updated_at MUST advance when the declared skill set changes."""

    def test_add_skill_bumps_generation(self, db_session):
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, user)
        _make_skill(db_session, "gen-add")
        db_session.commit()

        before = _backdate(db_session, cb.id)

        app = _make_app(db_session, user.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/skills", json={"slug": "gen-add"})
        assert r.status_code == 201, r.text

        after = _generation(db_session, cb.id)
        assert after > before, (
            "adding a skill must advance the parent generation token " f"(before={before!r} after={after!r})"
        )

    def test_remove_skill_bumps_generation(self, db_session):
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, user)
        skill = _make_skill(db_session, "gen-rm")
        db_session.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        before = _backdate(db_session, cb.id)

        app = _make_app(db_session, user.id)
        with TestClient(app) as client:
            r = client.delete(f"/api/cookbooks/{cb.id}/skills/gen-rm")
        assert r.status_code == 200, r.text

        after = _generation(db_session, cb.id)
        assert after > before, (
            "removing a skill must advance the parent generation token "
            f"(before={before!r} after={after!r})"
        )

    def test_reactivate_existing_skill_bumps_generation(self, db_session):
        """Re-adding a disabled skill (the reactivate branch) also changes the set."""
        user = _make_user(db_session)
        cb = _make_cookbook(db_session, user)
        skill = _make_skill(db_session, "gen-react")
        db_session.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="disabled"))
        db_session.commit()

        before = _backdate(db_session, cb.id)

        app = _make_app(db_session, user.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/skills", json={"slug": "gen-react"})
        assert r.status_code == 201, r.text
        assert r.json().get("reactivated") is True

        after = _generation(db_session, cb.id)
        assert after > before, "reactivating a skill must advance the generation token"


class TestGenerationTokenSyncPinWrite:
    """recipes_sync's pin-write must also advance the generation token."""

    def test_sync_pin_write_bumps_generation(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.recipes_sync import recipes_sync

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, user)
        skill = _make_skill(db_session, "gen-sync")
        # newer version exists upstream → sync will bump the pin
        db_session.add(
            SkillVersion(
                id=uuid4(),
                skill_id=skill.id,
                semver="0.2.0",
                tarball_path="/tmp/gen-sync-2.tar.gz",
                tarball_size_bytes=99,
                checksum_sha256="b" * 64,
            )
        )
        db_session.add(
            BundleSkill(
                bundle_id=cb.id,
                skill_id=skill.id,
                source="overridden",
                pinned_version="0.1.0",
            )
        )
        db_session.commit()

        before = _backdate(db_session, cb.id)

        ctx = AuthContext(
            scope="user",
            user_id=user.id,
            api_key_id=None,
            tier="pro",
        )
        result = recipes_sync(db=db_session, ctx=ctx, cookbook_id=str(cb.id))
        assert result.get("applied") is True, result

        after = _generation(db_session, cb.id)
        assert after > before, (
            "recipes_sync pin-write must advance the parent generation token "
            f"(before={before!r} after={after!r})"
        )

    def test_sync_noop_does_not_falsely_bump(self, db_session):
        """If nothing is outdated, the generation token must NOT move."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.recipes_sync import recipes_sync

        user = _make_user(db_session)
        cb = _make_cookbook(db_session, user)
        skill = _make_skill(db_session, "gen-noop")  # only 0.1.0 exists
        db_session.add(
            BundleSkill(
                bundle_id=cb.id,
                skill_id=skill.id,
                source="overridden",
                pinned_version="0.1.0",
            )
        )
        db_session.commit()

        before = _backdate(db_session, cb.id)

        ctx = AuthContext(scope="user", user_id=user.id, api_key_id=None, tier="pro")
        recipes_sync(db=db_session, ctx=ctx, cookbook_id=str(cb.id))

        after = _generation(db_session, cb.id)
        assert after == before, "a no-op sync must not advance the generation token"
