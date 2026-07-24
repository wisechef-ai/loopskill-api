"""Tests for public discovery endpoints — search and trending must work
without an x-api-key header so agents can discover before subscribing.

Covers G2 (LarryBrain spec §4.1: search is no-auth, trending is no-auth).

Tests the FULL middleware stack — that's the contract being verified, not
just the route handlers.
"""
from __future__ import annotations

import uuid
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.middleware import APIKeyMiddleware
from app.models import Base, Skill


# ── DB fixtures ─────────────────────────────────────────────────────────────

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
def app_with_middleware(db) -> FastAPI:
    """Build a FastAPI app with the real APIKeyMiddleware + skills routes."""
    from app.routes import router as skills_router
    from app.skill_routes import router as skill_router  # Phase E: search/trending moved

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    def _override_get_db():
        try:
            yield db
        finally:
            pass
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(skills_router)  # router already has prefix="/api"
    app.include_router(skill_router, prefix="/api")  # Phase E: /skills/search + /skills/trending
    return app


@pytest.fixture
def client(app_with_middleware) -> TestClient:
    return TestClient(app_with_middleware)


def _make_skill(db, slug, title="Skill", category="devops", is_public=True, install_count=10):
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=title,
        description=f"A {category} skill for testing public discovery.",
        category=category,
        tier="operator",
        is_public=is_public,
        install_count=install_count,
        rating_avg=4.0,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ── Tests ───────────────────────────────────────────────────────────────────

class TestPublicSearch:
    def test_search_works_without_api_key(self, client, db):
        """G2: /api/skills/search must be reachable with NO x-api-key header."""
        _make_skill(db, slug="public-search-1", category="devops")

        # Explicitly NO headers
        resp = client.get("/api/skills/search?q=devops")
        assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data
        assert any(s["slug"] == "public-search-1" for s in data["results"])

    def test_search_filters_by_category_unauthenticated(self, client, db):
        _make_skill(db, slug="cat-search-marketing", category="marketing")
        _make_skill(db, slug="cat-search-devops", category="devops")

        resp = client.get("/api/skills/search?category=marketing")
        assert resp.status_code == 200
        slugs = [s["slug"] for s in resp.json()["results"]]
        assert "cat-search-marketing" in slugs
        assert "cat-search-devops" not in slugs


class TestPublicTrending:
    def test_trending_works_without_api_key(self, client, db):
        """G2: /api/skills/trending must be public for agent discovery."""
        _make_skill(db, slug="trend-public-1", install_count=500)

        resp = client.get("/api/skills/trending")
        assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data


class TestStillProtected:
    """Verify we didn't accidentally open up authenticated endpoints."""

    def test_install_still_requires_auth(self, client):
        """install must NOT be public — it gates premium content access."""
        resp = client.get("/api/skills/install?slug=anything&mode=files")
        # 401 (no key, blocked by middleware) is the expected outcome.
        # 404 also acceptable if the route does its own existence check first
        # (depends on middleware ordering). What we DON'T want is 200.
        assert resp.status_code in (401, 404, 403), (
            f"install endpoint returned {resp.status_code} without auth — "
            f"should be 401/403/404, never 200"
        )
