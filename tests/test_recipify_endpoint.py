"""POST /api/recipify endpoint — v7 Phase G."""
from __future__ import annotations

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
from app.models import Base, Cookbook, CookbookSkill, Skill, User


_GOOD = """---
name: scrape-bot
description: A web-scraping skill that crawls and extracts structured data.
---
Scrape and ETL pipeline for analytics.
"""


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


def _make_user(db: Session, *, tier: str | None, status: str | None = "active") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="T",
        email=f"{uid}@x.example",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(user)
    db.flush()
    return user


def _make_app(db: Session, *, api_key_user_id, is_admin: bool = False) -> FastAPI:
    from app.recipify_routes import router as recipify_router

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
            request.state.api_key_user_id = None if is_admin else _uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(recipify_router)
    return app


# ── Tests ────────────────────────────────────────────────────────────────


def test_free_tier_returns_401(db_session):
    user = _make_user(db_session, tier="free")
    db_session.commit()

    app = _make_app(db_session, api_key_user_id=user.id)
    with TestClient(app) as client:
        r = client.post("/api/recipify", json={"slug": "scrape-bot", "content": _GOOD})
    assert r.status_code == 401


def test_cook_user_creates_cookbook_skill(db_session):
    user = _make_user(db_session, tier="cook")
    cb = Cookbook(id=uuid4(), name="My", cookbook_owner=user.id)
    db_session.add(cb)
    db_session.commit()

    app = _make_app(db_session, api_key_user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/recipify",
            json={
                "slug": "scrape-bot",
                "content": _GOOD,
                "target_cookbook_id": str(cb.id),
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "scrape-bot"
    assert body["status"] == "created"
    assert body["category"] == "data"

    skill = db_session.query(Skill).filter(Skill.slug == "scrape-bot").first()
    assert skill is not None
    cs = (
        db_session.query(CookbookSkill)
        .filter(
            CookbookSkill.cookbook_id == cb.id,
            CookbookSkill.skill_id == skill.id,
        )
        .first()
    )
    assert cs is not None
    assert cs.source == "custom-added"


def test_rerun_with_same_slug_is_idempotent_and_updated(db_session):
    user = _make_user(db_session, tier="cook")
    cb = Cookbook(id=uuid4(), name="My", cookbook_owner=user.id)
    db_session.add(cb)
    db_session.commit()

    app = _make_app(db_session, api_key_user_id=user.id)
    with TestClient(app) as client:
        r1 = client.post(
            "/api/recipify",
            json={
                "slug": "scrape-bot",
                "content": _GOOD,
                "target_cookbook_id": str(cb.id),
            },
        )
        r2 = client.post(
            "/api/recipify",
            json={
                "slug": "scrape-bot",
                "content": _GOOD,
                "target_cookbook_id": str(cb.id),
            },
        )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["status"] == "created"
    assert r2.json()["status"] == "updated"
    # Only one CookbookSkill row for the slug.
    skill = db_session.query(Skill).filter(Skill.slug == "scrape-bot").first()
    rows = (
        db_session.query(CookbookSkill)
        .filter(CookbookSkill.skill_id == skill.id)
        .all()
    )
    assert len(rows) == 1


def test_invalid_frontmatter_returns_422(db_session):
    user = _make_user(db_session, tier="cook")
    db_session.commit()

    app = _make_app(db_session, api_key_user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/recipify",
            json={"slug": "ok-slug", "content": "no frontmatter at all"},
        )
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "invalid_frontmatter"


def test_cook_with_subrecipe_target_blocked_403(db_session):
    user = _make_user(db_session, tier="cook")
    cb = Cookbook(id=uuid4(), name="My", cookbook_owner=user.id)
    db_session.add(cb)
    db_session.commit()

    app = _make_app(db_session, api_key_user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/recipify",
            json={
                "slug": "scrape-bot",
                "content": _GOOD,
                "target_cookbook_id": str(cb.id),
                "target_subrecipe_id": str(uuid4()),
            },
        )
    assert r.status_code == 403
    assert r.json()["detail"] == "subrecipe_requires_operator"


def test_operator_with_subrecipe_target_succeeds(db_session):
    user = _make_user(db_session, tier="operator")
    cb = Cookbook(id=uuid4(), name="My", cookbook_owner=user.id)
    db_session.add(cb)
    db_session.commit()

    app = _make_app(db_session, api_key_user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/recipify",
            json={
                "slug": "scrape-bot",
                "content": _GOOD,
                "target_cookbook_id": str(cb.id),
                "target_subrecipe_id": str(uuid4()),
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "scrape-bot"
    assert body["status"] in {"created", "updated"}
