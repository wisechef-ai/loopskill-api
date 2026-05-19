"""Tests for Plan v5.4 §A.1 (counter integrity) and §A.8 (Stripe tier semantics).

A.1: /api/skills/search and /api/skills/{slug} return install_count_total
     and install_count_7d aggregated from install_events.

A.8: /api/skills/access reflects subscription-tier capability ladder:
     Cook → all skills, Operator → +forks, Studio → +buckets.
"""
from __future__ import annotations

import hashlib
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
from app.models import APIKey, Base, InstallEvent, Skill, User


# ── DB fixtures (module-scoped engine, per-test rollback) ───────────────────

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
    from app.routes import router as skills_router
    from app.skill_routes import router as skill_router  # Phase E: skills moved

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(skills_router)
    app.include_router(skill_router, prefix="/api")  # Phase E: /skills/*
    return app


@pytest.fixture
def client(app_with_middleware) -> TestClient:
    return TestClient(app_with_middleware)


def _make_skill(db, slug="skill-x", title="Skill X", tier=None, is_public=True):
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=title,
        description="desc",
        category="devops",
        tier=tier,
        is_public=is_public,
        install_count=0,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_install(db, skill_id, when: datetime):
    ev = InstallEvent(
        id=uuid.uuid4(),
        skill_id=skill_id,
        skill_slug=None,
        version_semver="1.0.0",
        created_at=when,
    )
    db.add(ev)


def _make_user_with_key(db, *, tier: str | None, status: str = "active") -> str:
    user = User(
        id=uuid.uuid4(),
        display_name="Tester",
        email="t@example.com",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(user)
    db.flush()

    raw_key = "rec_" + uuid.uuid4().hex
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    api_key = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_prefix=raw_key[:8],
        key_hash=key_hash,
        is_active=True,
    )
    db.add(api_key)
    db.commit()
    return raw_key


# ── A.1: install counter fields ─────────────────────────────────────────────

class TestInstallCounters:
    def test_search_returns_total_and_7d_counts(self, client, db):
        skill = _make_skill(
            db, slug="counter-search-1", title="Counter search target"
        )
        now = datetime.now(timezone.utc)
        # 3 recent installs (within 7d) + 2 old installs (>7d)
        for _ in range(3):
            _make_install(db, skill.id, now - timedelta(days=1))
        for _ in range(2):
            _make_install(db, skill.id, now - timedelta(days=14))
        db.commit()

        resp = client.get("/api/skills/search?q=Counter search target")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        match = next(s for s in body["results"] if s["slug"] == "counter-search-1")
        assert match["install_count_total"] == 5
        assert match["install_count_7d"] == 3

    def test_detail_returns_total_and_7d_counts(self, client, db):
        skill = _make_skill(db, slug="counter-detail-1")
        now = datetime.now(timezone.utc)
        _make_install(db, skill.id, now - timedelta(hours=1))
        _make_install(db, skill.id, now - timedelta(days=8))
        db.commit()

        resp = client.get("/api/skills/counter-detail-1")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["install_count_total"] == 2
        assert body["install_count_7d"] == 1

    def test_zero_installs_returns_zero_counts(self, client, db):
        _make_skill(db, slug="counter-zero")
        resp = client.get("/api/skills/counter-zero")
        assert resp.status_code == 200
        body = resp.json()
        assert body["install_count_total"] == 0
        assert body["install_count_7d"] == 0


# ── A.8: tier semantics ─────────────────────────────────────────────────────

class TestAccessTierSemantics:
    def test_anonymous_caller_no_subscription(self, client, db):
        _make_skill(db, slug="tier-cook-skill", tier="cook")
        resp = client.get("/api/skills/access?skill=tier-cook-skill")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # No x-api-key header → no tier → no access to cook-tier skill
        assert body["user_tier"] is None
        assert body["has_access"] is False
        assert body["fork_eligible"] is False
        assert body["bucket_eligible"] is False

    def test_operator_user_accesses_cook_skill(self, client, db):
        """Plan §A.8 acceptance: Operator gets access to a Cook-tier skill."""
        _make_skill(db, slug="tier-cook-for-op", tier="cook")
        op_key = _make_user_with_key(db, tier="operator")

        resp = client.get(
            "/api/skills/access?skill=tier-cook-for-op",
            headers={"x-api-key": op_key},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_tier"] == "operator"
        assert body["has_access"] is True
        assert body["fork_eligible"] is True
        assert body["bucket_eligible"] is False

    def test_cook_user_cannot_use_fork_eligible_capability(self, client, db):
        _make_skill(db, slug="tier-cook-fork", tier="cook")
        cook_key = _make_user_with_key(db, tier="cook")

        # Plain access — Cook can read a Cook-tier skill.
        resp = client.get(
            "/api/skills/access?skill=tier-cook-fork",
            headers={"x-api-key": cook_key},
        )
        assert resp.json()["has_access"] is True

        # fork_eligible=true requires Operator+, so a Cook subscriber is denied.
        resp = client.get(
            "/api/skills/access?skill=tier-cook-fork&fork_eligible=true",
            headers={"x-api-key": cook_key},
        )
        body = resp.json()
        assert body["has_access"] is False
        assert body["fork_eligible"] is False

    def test_studio_user_has_bucket_capability(self, client, db):
        _make_skill(db, slug="tier-studio-target", tier="cook")
        studio_key = _make_user_with_key(db, tier="studio")
        resp = client.get(
            "/api/skills/access?skill=tier-studio-target",
            headers={"x-api-key": studio_key},
        )
        body = resp.json()
        assert body["user_tier"] == "studio"
        assert body["has_access"] is True
        assert body["fork_eligible"] is True
        assert body["bucket_eligible"] is True

    def test_inactive_subscription_treated_as_anonymous(self, client, db):
        _make_skill(db, slug="tier-canceled", tier="cook")
        canceled_key = _make_user_with_key(db, tier="operator", status="canceled")
        resp = client.get(
            "/api/skills/access?skill=tier-canceled",
            headers={"x-api-key": canceled_key},
        )
        body = resp.json()
        assert body["user_tier"] is None
        assert body["has_access"] is False
