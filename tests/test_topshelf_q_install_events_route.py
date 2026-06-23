"""Phase Q — tests for /api/skills/{slug}/install-events.

Covers:
  - Bucket aggregation (day-bucketed counts, correct length for window)
  - Window param validation: 7d, 30d accepted; others → 400
  - Empty install history → zero-filled buckets
  - total_all_time vs total_in_window distinction
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Base, InstallEvent, Skill, SkillVersion


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def set_pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture):
    conn = engine_fixture.connect()
    txn = conn.begin()
    _Session = sessionmaker(bind=conn)
    session = _Session()
    nested = conn.begin_nested()

    @sa_event.listens_for(session, "after_transaction_end")
    def restart_sp(s, t):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    yield session
    session.close()
    txn.rollback()
    conn.close()


def _make_skill(db, slug: str = "test-skill", tier: str = "pro") -> Skill:
    sk = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.title(),
        description="Test skill",
        tier=tier,
        category="devops",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    return sk


def _make_install_event(db, skill: Skill, days_ago: float = 0) -> InstallEvent:
    """Insert an InstallEvent `days_ago` days in the past."""
    ev_time = datetime.now(timezone.utc) - timedelta(days=days_ago)
    ev = InstallEvent(
        id=uuid4(),
        skill_id=skill.id,
        skill_slug=skill.slug,
        api_key_id=None,
        version_semver="1.0.0",
        client_ip="127.0.0.1",
        created_at=ev_time,
    )
    db.add(ev)
    db.flush()
    return ev


def _make_app(db_session):
    from app.skill_files_routes import router as files_router

    app = FastAPI()
    app.include_router(files_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestInstallEventsEndpoint:

    def test_7d_window_returns_7_buckets(self, db_session):
        """window=7d → 7 daily buckets."""
        from app.skill_files_routes import _INSTALL_EVENTS_CACHE
        _INSTALL_EVENTS_CACHE.clear()

        sk = _make_skill(db_session, slug="ie-7d-q")
        _make_install_event(db_session, sk, days_ago=1)

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/skills/ie-7d-q/install-events?window=7d")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_days"] == 7
        assert len(data["buckets"]) == 7

    def test_30d_window_returns_30_buckets(self, db_session):
        """window=30d → 30 daily buckets."""
        from app.skill_files_routes import _INSTALL_EVENTS_CACHE
        _INSTALL_EVENTS_CACHE.clear()

        sk = _make_skill(db_session, slug="ie-30d-q")
        _make_install_event(db_session, sk, days_ago=5)

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/skills/ie-30d-q/install-events?window=30d")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_days"] == 30
        assert len(data["buckets"]) == 30

    def test_invalid_window_returns_400(self, db_session):
        """window=60d (invalid) → 400."""
        from app.skill_files_routes import _INSTALL_EVENTS_CACHE
        _INSTALL_EVENTS_CACHE.clear()

        sk = _make_skill(db_session, slug="ie-bad-window-q")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/skills/ie-bad-window-q/install-events?window=60d")
        assert resp.status_code == 400

    def test_invalid_window_no_param_defaults_7d(self, db_session):
        """No window param → defaults to 7d."""
        from app.skill_files_routes import _INSTALL_EVENTS_CACHE
        _INSTALL_EVENTS_CACHE.clear()

        sk = _make_skill(db_session, slug="ie-default-q")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/skills/ie-default-q/install-events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_days"] == 7

    def test_empty_history_zero_buckets(self, db_session):
        """No install events → all bucket counts are 0."""
        from app.skill_files_routes import _INSTALL_EVENTS_CACHE
        _INSTALL_EVENTS_CACHE.clear()

        sk = _make_skill(db_session, slug="ie-empty-q")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/skills/ie-empty-q/install-events?window=7d")
        assert resp.status_code == 200
        data = resp.json()
        assert all(b["count"] == 0 for b in data["buckets"])
        assert data["total_in_window"] == 0
        assert data["total_all_time"] == 0

    def test_bucket_aggregation_counts(self, db_session):
        """Events in the last 2 days show up in buckets; old event in total_all_time."""
        from app.skill_files_routes import _INSTALL_EVENTS_CACHE
        _INSTALL_EVENTS_CACHE.clear()

        sk = _make_skill(db_session, slug="ie-counts-q")
        _make_install_event(db_session, sk, days_ago=0)   # today
        _make_install_event(db_session, sk, days_ago=0)   # today (2nd)
        _make_install_event(db_session, sk, days_ago=40)  # outside 30d window

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/skills/ie-counts-q/install-events?window=30d")
        assert resp.status_code == 200
        data = resp.json()

        total_in_window = data["total_in_window"]
        total_all_time = data["total_all_time"]

        assert total_in_window == 2   # only events inside 30d window
        assert total_all_time == 3    # includes the old event

        # Today's bucket should have count 2
        today_buckets = [b for b in data["buckets"] if b["count"] > 0]
        assert len(today_buckets) >= 1
        assert max(b["count"] for b in today_buckets) == 2

    def test_bucket_date_format(self, db_session):
        """Each bucket has a date in ISO-8601 format (YYYY-MM-DD)."""
        from app.skill_files_routes import _INSTALL_EVENTS_CACHE
        _INSTALL_EVENTS_CACHE.clear()

        sk = _make_skill(db_session, slug="ie-dateformat-q")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/api/skills/ie-dateformat-q/install-events?window=7d")
        assert resp.status_code == 200
        data = resp.json()
        import re
        for bucket in data["buckets"]:
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", bucket["date"]), f"Bad date: {bucket['date']}"

    def test_skill_not_found_returns_404(self, db_session):
        """Skill not found → 404."""
        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/nonexistent-ie-q/install-events?window=7d")
        assert resp.status_code == 404
