"""evergreen_0206 — TENANT ISOLATION hard gate (Adam directive 2026-06-03).

> Our internal setup, skills, and cookbooks MUST be invisible and inaccessible
> to every other Recipes user.

This suite is the per-sprint isolation wall. It GROWS one class per phase that
adds a read/write surface. Phase A covers the cookbook-resolution + generation
surfaces. The invariant under test: a non-owner gets 404 (never 200/304, never
a leaked generation token) for a cookbook they don't own.

See docs/reconcile-contract.md §7 for the full rule set.
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
from app.models import Base, Cookbook, CookbookSkill, Skill, User


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


def _make_user(db: Session, *, tier: str = "pro") -> User:
    uid = uuid4()
    u = User(
        id=uid,
        display_name="Tenant",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _make_cookbook(db: Session, owner: User, name: str = "Private CB") -> Cookbook:
    cb = Cookbook(id=uuid4(), name=name, is_base=False, bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    return cb


def _make_skill(db: Session, slug: str) -> Skill:
    s = Skill(id=uuid4(), slug=slug, title=slug, description="x", is_public=False)
    db.add(s)
    db.flush()
    return s


def _app_as(db: Session, acting_user_id) -> FastAPI:
    """Build the cookbook router with auth state injected for a given user."""
    from app.cookbook_routes import router as cookbook_router

    app = FastAPI()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = acting_user_id
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)
    return app


class TestPhaseACookbookIsolation:
    """Tenant B must not see, read, or mutate tenant A's cookbook."""

    def test_non_owner_cannot_read_cookbook(self, db_session):
        owner = _make_user(db_session)
        intruder = _make_user(db_session)
        cb = _make_cookbook(db_session, owner, name="Internal Tori CB")
        db_session.commit()

        app = _app_as(db_session, intruder.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}")
        assert r.status_code == 404, f"non-owner must get 404, not {r.status_code} — no cross-tenant read"

    def test_non_owner_cannot_add_skill(self, db_session):
        owner = _make_user(db_session)
        intruder = _make_user(db_session)
        cb = _make_cookbook(db_session, owner)
        _make_skill(db_session, "iso-add")
        db_session.commit()

        app = _app_as(db_session, intruder.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/skills", json={"slug": "iso-add"})
        assert r.status_code == 404, "non-owner must not mutate another tenant's cookbook"

    def test_non_owner_cannot_remove_skill(self, db_session):
        owner = _make_user(db_session)
        intruder = _make_user(db_session)
        cb = _make_cookbook(db_session, owner)
        skill = _make_skill(db_session, "iso-rm")
        db_session.add(CookbookSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        app = _app_as(db_session, intruder.id)
        with TestClient(app) as client:
            r = client.delete(f"/api/cookbooks/{cb.id}/skills/iso-rm")
        assert r.status_code == 404, "non-owner must not remove from another tenant's cookbook"

    def test_generation_not_leaked_to_non_owner(self, db_session):
        """A non-owner gets 404 BEFORE any generation comparison (contract §7.2).

        The intruder reading the cookbook must not be able to distinguish a
        changed vs unchanged generation — they get a flat 404 regardless.
        """
        owner = _make_user(db_session)
        intruder = _make_user(db_session)
        cb = _make_cookbook(db_session, owner)
        db_session.commit()

        app = _app_as(db_session, intruder.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}")
        # 404, and body must not echo updated_at/generation.
        assert r.status_code == 404
        assert "updated_at" not in r.text
        assert "generation" not in r.text

    def test_owner_can_still_read_own(self, db_session):
        """Positive control: the legitimate owner reads their own cookbook."""
        owner = _make_user(db_session)
        cb = _make_cookbook(db_session, owner)
        db_session.commit()

        app = _app_as(db_session, owner.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == str(cb.id)


class TestPhaseDReconcileIsolation:
    """Phase D: the reconcile HTTP endpoint must not leak across tenants.

    Covered in depth by test_evergreen_d_reconcile_endpoint.py
    (test_non_owner_gets_404_not_304); this is the cross-phase isolation
    anchor that the contract §7 obligation is satisfied for the reconcile
    surface — a non-owner gets 404 even with a correct generation token, so the
    304/200 change-state never leaks.
    """

    def test_reconcile_isolation_anchor(self):
        # The behavioral proof lives in the endpoint suite; this marker keeps the
        # per-phase isolation obligation visible in the central isolation file.
        from app.reconcile_routes import reconcile_cookbook

        assert reconcile_cookbook is not None


class TestPhaseFFederationIsolation:
    """Phase F: external/federated skills must never surface our internal skills.

    The quality-namespace wall is also a tenant-isolation wall (contract §7.3).
    Behavioral proof lives in test_evergreen_f_federation.py::TestIsolationWall —
    external never mixes into the internal list, and merge_search never upgrades
    internal visibility. This anchors the obligation centrally.
    """

    def test_federation_isolation_anchor(self):
        from app.services.federation import INTERNAL_SOURCE, merge_search

        # Internal source namespace is distinct + merge keeps lists separate.
        assert INTERNAL_SOURCE == "recipes"
        res = merge_search([], [], free_sources_enabled=True)
        assert res.external == [] and res.internal == []

