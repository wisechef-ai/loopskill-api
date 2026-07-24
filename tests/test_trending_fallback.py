"""RCP-11: trending endpoint must not return empty when install events exist.

Bug: /api/skills/trending returned total=0 results=[] in production despite
121 install events in telemetry_events. Root cause was a date-only filter
(period=week ⇒ created_at >= now-7d) that excluded every event because the
most recent install was 13 days old.

Fix: when the requested window has no install events, transparently widen
the lookback (day → week → month → all-time) so trending degrades gracefully
on quiet stretches but still surfaces real install history.

These tests cover:
  - empty DB still returns the empty shape (no false positives)
  - period=week with only week-old events ⇒ those events surface
  - period=week with only month-old events ⇒ widens to month, surfaces them
  - period=day with only month-old events ⇒ widens past week to month
  - period=month with only year-old events ⇒ widens to all-time
  - private skills excluded even when install events exist for them
  - ordering: most installs first
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.middleware import APIKeyMiddleware
from app.models import Base, Skill, TelemetryEvent


# ── Fixtures ────────────────────────────────────────────────────────────────

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
def db(engine_fixture) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(bind=engine_fixture, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def app_with_middleware(db) -> FastAPI:
    from app.routes import router as skills_router
    from app.skill_routes import router as skill_router  # Phase E: trending moved

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(skills_router)
    app.include_router(skill_router, prefix="/api")  # Phase E: /skills/trending
    return app


@pytest.fixture()
def client(app_with_middleware) -> TestClient:
    return TestClient(app_with_middleware)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_skill(db, slug, *, is_public=True, category="devops"):
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug.title(),
        description=f"{slug} for testing",
        category=category,
        tier="operator",
        is_public=is_public,
        install_count=0,
        rating_avg=4.0,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_install(db, skill_slug, *, days_ago=0):
    """Insert a TelemetryEvent(install) created `days_ago` days in the past."""
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    evt = TelemetryEvent(
        id=uuid.uuid4(),
        event_type="install",
        skill_slug=skill_slug,
        created_at=when,
    )
    db.add(evt)
    db.commit()
    return evt


# ── Tests ───────────────────────────────────────────────────────────────────

class TestTrendingEmptyDB:
    def test_empty_db_returns_empty_shape(self, client):
        """No skills, no events ⇒ total=0, results=[]."""
        resp = client.get("/api/skills/trending")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["results"] == []

    def test_skills_but_no_install_events_returns_empty(self, client, db):
        """Skills exist but zero installs ⇒ empty (no fake trending)."""
        _make_skill(db, "no-installs")
        resp = client.get("/api/skills/trending")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestTrendingHappyPath:
    def test_week_window_with_recent_installs(self, client, db):
        """Installs within the requested window ⇒ no widening needed."""
        _make_skill(db, "fresh-skill")
        for _ in range(3):
            _make_install(db, "fresh-skill", days_ago=2)

        resp = client.get("/api/skills/trending?period=week")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"][0]["slug"] == "fresh-skill"

    def test_orders_by_install_count_descending(self, client, db):
        _make_skill(db, "popular")
        _make_skill(db, "less-popular")
        for _ in range(10):
            _make_install(db, "popular", days_ago=1)
        for _ in range(2):
            _make_install(db, "less-popular", days_ago=1)

        resp = client.get("/api/skills/trending?period=week")
        body = resp.json()
        slugs = [s["slug"] for s in body["results"]]
        assert slugs == ["popular", "less-popular"]


class TestTrendingFallback:
    """RCP-11 regression: widen the window when the requested one is empty."""

    def test_week_widens_to_month_when_week_empty(self, client, db):
        """Bug repro: week-old request, but only month-old events exist."""
        _make_skill(db, "stale-but-installed")
        # All events 13 days ago — outside week (7d) but inside month (30d)
        for _ in range(5):
            _make_install(db, "stale-but-installed", days_ago=13)

        resp = client.get("/api/skills/trending?period=week")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1, (
            f"week-period with only month-old events should widen to month, "
            f"got {body}"
        )
        assert body["results"][0]["slug"] == "stale-but-installed"

    def test_day_widens_through_week_to_month(self, client, db):
        """day → week → month progression when only month-old events exist."""
        _make_skill(db, "old-installs")
        for _ in range(2):
            _make_install(db, "old-installs", days_ago=20)

        resp = client.get("/api/skills/trending?period=day")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"][0]["slug"] == "old-installs"

    def test_month_widens_to_all_time_when_only_ancient_installs(self, client, db):
        """month → all-time fallback when every event is older than 30 days."""
        _make_skill(db, "ancient")
        for _ in range(3):
            _make_install(db, "ancient", days_ago=400)  # > 1 year old

        resp = client.get("/api/skills/trending?period=month")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"][0]["slug"] == "ancient"

    def test_widening_prefers_narrowest_non_empty_window(self, client, db):
        """If day has hits, don't widen — even though week/month also have hits."""
        _make_skill(db, "today-hot")
        _make_skill(db, "month-old")
        _make_install(db, "today-hot", days_ago=0)
        for _ in range(50):  # more total events, but they're old
            _make_install(db, "month-old", days_ago=20)

        resp = client.get("/api/skills/trending?period=day")
        body = resp.json()
        # day window has events ⇒ no widening ⇒ only today-hot surfaces
        assert body["total"] == 1
        assert body["results"][0]["slug"] == "today-hot"


class TestTrendingVisibility:
    def test_private_skills_excluded_even_with_installs(self, client, db):
        """is_public=False skills must not appear in trending."""
        _make_skill(db, "secret-skill", is_public=False)
        for _ in range(50):
            _make_install(db, "secret-skill", days_ago=1)

        resp = client.get("/api/skills/trending?period=week")
        body = resp.json()
        assert body["total"] == 0

    def test_install_for_unknown_slug_does_not_break_query(self, client, db):
        """Telemetry referencing a deleted/renamed slug must not surface."""
        _make_install(db, "deleted-skill", days_ago=1)
        # No matching Skill row.
        resp = client.get("/api/skills/trending?period=week")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestTrendingPagination:
    def test_pagination_works_after_widening(self, client, db):
        for i in range(5):
            slug = f"pop-{i}"
            _make_skill(db, slug)
            for _ in range(i + 1):
                _make_install(db, slug, days_ago=15)  # forces month widening

        resp = client.get("/api/skills/trending?period=week&page=1&page_size=2")
        body = resp.json()
        assert body["total"] == 5
        assert len(body["results"]) == 2
        assert body["page"] == 1
        assert body["page_size"] == 2

        resp2 = client.get("/api/skills/trending?period=week&page=2&page_size=2")
        body2 = resp2.json()
        assert body2["total"] == 5
        assert len(body2["results"]) == 2
        # No overlap between page 1 and page 2
        page1_slugs = {s["slug"] for s in body["results"]}
        page2_slugs = {s["slug"] for s in body2["results"]}
        assert page1_slugs.isdisjoint(page2_slugs)
