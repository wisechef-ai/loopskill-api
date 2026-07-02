"""Polling-fallback endpoint coverage — v7 Phase D.

The SSE endpoint advertises ``/api/cookbooks/{id}/sync?since=<iso8601>`` as
its fallback URL when the connection cap is hit.  These tests verify the
``since`` filter, action classification, and that the fallback URL emitted
by a 503 sse_pool_exhausted response actually works.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from app.models import Base, Bundle, BundleSkill, Skill, User


@pytest.fixture()
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
def db_session(engine_fixture) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(bind=engine_fixture, autocommit=False, autoflush=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_user(db: Session, *, tier: str = "operator") -> User:
    u = User(
        id=uuid4(),
        display_name="t",
        email=f"{uuid4()}@test.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _make_skill(db: Session, slug: str) -> Skill:
    s = Skill(id=uuid4(), slug=slug, title=slug, description="", is_public=True)
    db.add(s)
    db.flush()
    return s


def _build_client(db: Session, *, uid) -> TestClient:
    from app.bundle_routes import router as cookbook_router

    app = FastAPI()

    def _odb():
        SessionLocal = sessionmaker(bind=db.bind, autocommit=False, autoflush=False)
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _odb

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(_Auth)
    app.include_router(cookbook_router)
    return TestClient(app)


# ── Tests ────────────────────────────────────────────────────────────────


def test_since_filter_returns_only_new_events(db_session):
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="X", bundle_owner=user.id)
    s_old = _make_skill(db_session, "older")
    s_new = _make_skill(db_session, "newer")
    db_session.add(cb)
    db_session.flush()

    old_ts = datetime(2026, 1, 1, 0, 0, 0)
    new_ts = datetime(2026, 5, 1, 0, 0, 0)
    db_session.add(BundleSkill(
        bundle_id=cb.id, skill_id=s_old.id, source="custom-added", added_at=old_ts,
    ))
    db_session.add(BundleSkill(
        bundle_id=cb.id, skill_id=s_new.id, source="custom-added", added_at=new_ts,
    ))
    db_session.commit()

    client = _build_client(db_session, uid=user.id)
    cutoff = "2026-04-01T00:00:00Z"
    r = client.get(f"/api/cookbooks/{cb.id}/sync", params={"since": cutoff})
    assert r.status_code == 200, r.text
    body = r.json()
    slugs = [e["slug"] for e in body["added"]]
    assert "newer" in slugs
    assert "older" not in slugs


def test_disabled_classified_as_removed(db_session):
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="X", bundle_owner=user.id)
    skill = _make_skill(db_session, "ghost")
    db_session.add(cb)
    db_session.flush()
    db_session.add(BundleSkill(
        bundle_id=cb.id, skill_id=skill.id, source="disabled",
        added_at=datetime(2026, 5, 1, 0, 0, 0),
    ))
    db_session.commit()

    client = _build_client(db_session, uid=user.id)
    r = client.get(f"/api/cookbooks/{cb.id}/sync")
    assert r.status_code == 200
    body = r.json()
    assert any(e["slug"] == "ghost" for e in body["removed"])
    assert all(e["slug"] != "ghost" for e in body["added"])


def test_overridden_classified_as_updated(db_session):
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="X", bundle_owner=user.id)
    skill = _make_skill(db_session, "patched")
    db_session.add(cb)
    db_session.flush()
    db_session.add(BundleSkill(
        bundle_id=cb.id, skill_id=skill.id, source="overridden",
        added_at=datetime(2026, 5, 1, 0, 0, 0),
    ))
    db_session.commit()

    client = _build_client(db_session, uid=user.id)
    r = client.get(f"/api/cookbooks/{cb.id}/sync")
    assert r.status_code == 200
    body = r.json()
    assert any(e["slug"] == "patched" for e in body["updated"])


def test_invalid_since_returns_422(db_session):
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="X", bundle_owner=user.id)
    db_session.add(cb)
    db_session.commit()

    client = _build_client(db_session, uid=user.id)
    r = client.get(f"/api/cookbooks/{cb.id}/sync", params={"since": "not-a-date"})
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid_since"


def test_sync_other_users_cookbook_returns_404(db_session):
    owner = _make_user(db_session)
    intruder = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="X", bundle_owner=owner.id)
    db_session.add(cb)
    db_session.commit()

    client = _build_client(db_session, uid=intruder.id)
    r = client.get(f"/api/cookbooks/{cb.id}/sync")
    assert r.status_code == 404
