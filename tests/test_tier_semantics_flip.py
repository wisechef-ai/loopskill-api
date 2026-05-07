"""WIS-902: Tier semantics flip — Cook gets full catalog install.

Integration tests:
1. Cook tier installs an operator-tagged skill → 200 OK with rate-limit headers
2. Cook tier hits 101st install → 429 with upgrade copy in body
3. Free tier rate limited at 5 installs
4. Operator tier unlimited installs (no rate-limit headers)
5. Rate limit resets daily
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.middleware import APIKeyMiddleware
from app.models import (
    APIKey,
    Base,
    InstallEvent,
    Skill,
    SkillVersion,
    User,
)


# ── DB fixtures ────────────────────────────────────────────────────────────

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
def patched_client(db, engine_fixture) -> TestClient:
    """TestClient with middleware + patched SessionLocal so auth middleware
    reads from the same in-memory test DB."""
    from app.routes import router as skills_router
    import app.database as db_mod

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    # Wire the test engine into both the dependency injection AND the
    # middleware's direct SessionLocal() calls.
    test_session_factory = sessionmaker(
        bind=engine_fixture, autocommit=False, autoflush=False,
    )

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(skills_router)

    with patch.object(db_mod, "SessionLocal", test_session_factory):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_skill(db, slug="test-skill", title="Test Skill", tier="operator",
                is_public=True):
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
    db.flush()
    # Add a version so install_skill doesn't 404
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=s.id,
        semver="1.0.0",
        changelog="init",
        checksum_sha256="abc123",
        tarball_size_bytes=1024,
    )
    db.add(v)
    db.commit()
    return s


def _make_user_with_key(db, *, tier: str | None, status: str = "active") -> tuple[str, uuid.UUID]:
    """Returns (raw_api_key, api_key_id)."""
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
    return raw_key, api_key.id


def _make_install_event(db, skill_id, api_key_id=None, when=None):
    """Create an install event to simulate previous installs."""
    if when is None:
        when = datetime.now(timezone.utc)
    ev = InstallEvent(
        id=uuid.uuid4(),
        skill_id=skill_id,
        skill_slug="some-skill",
        api_key_id=api_key_id,
        version_semver="1.0.0",
        created_at=when,
    )
    db.add(ev)


# ── Test 1: Cook installs operator-tagged skill → 200 ─────────────────────

class TestCookFullCatalog:
    def test_cook_installs_operator_skill(self, patched_client, db):
        """WIS-902 acceptance: Cook tier installs operator-tagged skill → 200."""
        skill = _make_skill(db, slug="graphify-op", title="Graphify", tier="operator")
        cook_key, _ = _make_user_with_key(db, tier="cook")

        resp = patched_client.get(
            "/api/skills/install?slug=graphify-op",
            headers={"x-api-key": cook_key},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["slug"] == "graphify-op"
        # Rate-limit headers should be present for Cook tier
        assert resp.headers.get("X-RateLimit-Limit") == "100"

    def test_cook_installs_cook_skill(self, patched_client, db):
        """Cook installs cook-tier skill → 200."""
        _make_skill(db, slug="cook-skill-1", title="Cook Skill", tier="cook")
        cook_key, _ = _make_user_with_key(db, tier="cook")

        resp = patched_client.get(
            "/api/skills/install?slug=cook-skill-1",
            headers={"x-api-key": cook_key},
        )
        assert resp.status_code == 200, resp.text


# ── Test 2: Cook hits rate limit → 429 ────────────────────────────────────

class TestCookRateLimit:
    def test_cook_rate_limit_429(self, patched_client, db):
        """WIS-902: Cook tier hits 101st install → 429 with upgrade copy."""
        # Create 101 skills with versions
        skills = []
        for i in range(101):
            skills.append(_make_skill(db, slug=f"rl-skill-{i}", title=f"RL Skill {i}"))

        cook_key, ak_id = _make_user_with_key(db, tier="cook")

        # Simulate 100 previous installs today
        for i in range(100):
            _make_install_event(db, skills[i].id, api_key_id=ak_id)
        db.commit()

        # 101st install should hit the limit
        resp = patched_client.get(
            "/api/skills/install?slug=rl-skill-100",
            headers={"x-api-key": cook_key},
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "Upgrade to Operator" in body["detail"]
        assert body["tier"] == "cook"
        assert body["limit"] == 100
        # Check standard rate-limit headers
        assert resp.headers.get("X-RateLimit-Limit") == "100"
        assert resp.headers.get("X-RateLimit-Remaining") == "0"
        assert "Retry-After" in resp.headers


# ── Test 3: Free tier rate limited at 5 ────────────────────────────────────

class TestFreeTierRateLimit:
    def test_free_tier_5_installs_per_day(self, patched_client, db):
        """WIS-902: Free tier limited to 5 installs/day."""
        skills = []
        for i in range(7):
            skills.append(_make_skill(db, slug=f"free-skill-{i}", title=f"Free {i}"))

        free_key, ak_id = _make_user_with_key(db, tier="free")

        # Simulate 5 previous installs today
        for i in range(5):
            _make_install_event(db, skills[i].id, api_key_id=ak_id)
        db.commit()

        # 6th install should hit the limit
        resp = patched_client.get(
            "/api/skills/install?slug=free-skill-5",
            headers={"x-api-key": free_key},
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["limit"] == 5


# ── Test 4: Operator unlimited installs ────────────────────────────────────

class TestOperatorUnlimited:
    def test_operator_no_rate_limit(self, patched_client, db):
        """WIS-902: Operator tier has unlimited installs (no rate-limit headers)."""
        _make_skill(db, slug="op-skill-1", title="Op Skill", tier="operator")
        op_key, _ = _make_user_with_key(db, tier="operator")

        resp = patched_client.get(
            "/api/skills/install?slug=op-skill-1",
            headers={"x-api-key": op_key},
        )
        assert resp.status_code == 200, resp.text
        # Operator should NOT have rate-limit headers (unlimited)
        assert "X-RateLimit-Limit" not in resp.headers


# ── Test 5: Rate limit resets daily ───────────────────────────────────────

class TestRateLimitReset:
    def test_old_installs_dont_count(self, patched_client, db):
        """WIS-902: Installs from yesterday don't count toward today's limit."""
        skills = []
        for i in range(7):
            skills.append(_make_skill(db, slug=f"reset-skill-{i}", title=f"Reset {i}"))

        free_key, ak_id = _make_user_with_key(db, tier="free")

        # 5 installs from yesterday (should not count)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        for i in range(5):
            _make_install_event(db, skills[i].id, api_key_id=ak_id, when=yesterday)
        db.commit()

        # Today's install should work (yesterday's don't count)
        resp = patched_client.get(
            "/api/skills/install?slug=reset-skill-5",
            headers={"x-api-key": free_key},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.headers.get("X-RateLimit-Limit") == "5"
        assert resp.headers.get("X-RateLimit-Remaining") == "4"
