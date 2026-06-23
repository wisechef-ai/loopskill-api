"""evergreen_0206 Phase D — reconcile HTTP endpoint (304 / 200 / isolation / 429)."""

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

from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Base, Cookbook, CookbookSkill, Skill, SkillVersion, User
from app.reconcile_routes import router as reconcile_router


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _r):
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


def _user(db: Session) -> User:
    uid = uuid4()
    u = User(
        id=uid,
        display_name="Owner",
        email=f"{uid}@test.example",
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _cookbook(db: Session, owner: User) -> Cookbook:
    cb = Cookbook(id=uuid4(), name="CB", is_base=False, bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    return cb


def _skill(db: Session, slug: str, semver="1.0.0", sha="a" * 64) -> Skill:
    s = Skill(id=uuid4(), slug=slug, title=slug, description="x", is_public=True)
    db.add(s)
    db.flush()
    db.add(
        SkillVersion(
            id=uuid4(),
            skill_id=s.id,
            semver=semver,
            tarball_path=f"/tmp/{slug}.tar.gz",
            tarball_size_bytes=10,
            checksum_sha256=sha,
        )
    )
    db.flush()
    return s


def _app(db: Session, *, acting_user_id, api_key_id="key-1") -> FastAPI:
    app = FastAPI()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    class InjectAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.auth_ctx = AuthContext(
                scope="user", user_id=acting_user_id, api_key_id=None, tier="pro"
            )
            request.state.api_key_id = api_key_id
            return await call_next(request)

    app.add_middleware(InjectAuth)
    app.include_router(reconcile_router)
    return app


def _generation(db: Session, cb_id) -> str:
    db.expire_all()
    cb = db.query(Cookbook).filter(Cookbook.id == cb_id).first()
    return cb.updated_at.isoformat() if cb.updated_at else ""


class TestReconcileEndpoint:
    def test_200_returns_diff_and_etag(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        skill = _skill(db, "ep-add")
        db.add(
            CookbookSkill(bundle_id=cb.id, skill_id=skill.id, source="overridden", pinned_version="1.0.0")
        )
        db.commit()

        app = _app(db, acting_user_id=owner.id)
        with TestClient(app) as client:
            # local is empty → the declared skill is an ADD
            r = client.post(f"/api/cookbooks/{cb.id}/reconcile", json={"local": [], "dry_run": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["diff"]["add"][0]["slug"] == "ep-add"
        assert r.headers.get("etag") is not None

    def test_304_on_matching_generation(self, db):
        owner = _user(db)
        cb = _cookbook(db, owner)
        db.commit()
        gen = _generation(db, cb.id)

        app = _app(db, acting_user_id=owner.id)
        with TestClient(app) as client:
            r = client.post(
                f"/api/cookbooks/{cb.id}/reconcile",
                json={"local": []},
                headers={"If-None-Match": f'"{gen}"'},
            )
        assert r.status_code == 304, f"matching generation must 304, got {r.status_code}"

    def test_non_owner_gets_404_not_304(self, db):
        """Tenant isolation: a non-owner must get 404, never 304/200."""
        owner = _user(db)
        intruder = _user(db)
        cb = _cookbook(db, owner)
        db.commit()
        gen = _generation(db, cb.id)

        app = _app(db, acting_user_id=intruder.id)
        with TestClient(app) as client:
            # Even WITH the correct generation, a non-owner must not learn 304.
            r = client.post(
                f"/api/cookbooks/{cb.id}/reconcile",
                json={"local": []},
                headers={"If-None-Match": f'"{gen}"'},
            )
        assert (
            r.status_code == 404
        ), f"non-owner must get 404 (no existence/change-state leak), got {r.status_code}"
        assert "304" not in str(r.status_code)

    def test_unknown_cookbook_404(self, db):
        owner = _user(db)
        db.commit()
        app = _app(db, acting_user_id=owner.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{uuid4()}/reconcile", json={"local": []})
        assert r.status_code == 404

    def test_abuse_ceiling_429(self, db, monkeypatch):
        """When the abuse ceiling trips, the endpoint returns 429 + Retry-After."""
        import app.reconcile_routes as rr
        from app.reconcile_abuse_ceiling import CeilingResult

        owner = _user(db)
        cb = _cookbook(db, owner)
        db.commit()

        # Force the ceiling to report blocked.
        monkeypatch.setattr(
            rr,
            "check_reconcile_abuse_ceiling",
            lambda key: CeilingResult(allowed=False, count=99, retry_after=300),
        )

        app = _app(db, acting_user_id=owner.id, api_key_id="spammer")
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/reconcile", json={"local": []})
        assert r.status_code == 429
        assert r.headers.get("retry-after") == "300"
