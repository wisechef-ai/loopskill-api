"""Tests for Issue #16: external_resources endpoint guards.

get_skill_external (/api/skills/{slug}/external) must return 404 for:
  - Private skills (is_public=False)
  - Archived skills (is_archived=True)
  - Non-existent slugs

And return 200 + list for public, non-archived skills.
"""

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Skill
from app.database import get_db
from app.routes import router
from app.skill_routes import router as skill_router  # Phase E: external moved


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture(scope="module")
def session(engine):
    from sqlalchemy.orm import Session
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture(scope="module")
def test_app(engine):
    """Minimal test app wired to in-memory DB."""
    app = FastAPI()
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.include_router(router)
    app.include_router(skill_router, prefix="/api")  # Phase E: /skills/{slug}/external moved
    app.dependency_overrides[get_db] = override_db
    return app


@pytest.fixture(scope="module")
def tc(test_app, session):
    """Seed the DB and return a TestClient."""
    from uuid import uuid4
    from datetime import datetime, timezone
    from app.config import settings

    # Public, non-archived skill with external_resources
    pub = Skill(
        id=uuid4(), slug="pub-skill", title="Public Skill",
        category="devops", is_public=True, is_archived=False,
        external_resources=[{"url": "https://example.com", "label": "Docs"}],
        created_at=datetime.now(timezone.utc),
    )
    # Private skill
    priv = Skill(
        id=uuid4(), slug="priv-skill", title="Private Skill",
        category="devops", is_public=False, is_archived=False,
        external_resources=[{"url": "https://secret.com", "label": "Secret"}],
        created_at=datetime.now(timezone.utc),
    )
    # Archived skill
    arch = Skill(
        id=uuid4(), slug="arch-skill", title="Archived Skill",
        category="devops", is_public=True, is_archived=True,
        external_resources=[{"url": "https://old.com", "label": "Old"}],
        created_at=datetime.now(timezone.utc),
    )
    session.add_all([pub, priv, arch])
    session.commit()

    with TestClient(test_app, headers={"x-api-key": settings.API_KEY}) as c:
        yield c


def test_public_skill_returns_resources(tc):
    resp = tc.get("/api/skills/pub-skill/external")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["url"] == "https://example.com"


def test_missing_slug_returns_404(tc):
    resp = tc.get("/api/skills/nonexistent-skill/external")
    assert resp.status_code == 404


def test_private_skill_returns_404(tc):
    """Private skill should return 404 — no oracle."""
    resp = tc.get("/api/skills/priv-skill/external")
    assert resp.status_code == 404, (
        f"Expected 404 for private skill, got {resp.status_code}: {resp.json()}"
    )


def test_archived_skill_returns_404(tc):
    """Archived skill should return 404 — no oracle."""
    resp = tc.get("/api/skills/arch-skill/external")
    assert resp.status_code == 404, (
        f"Expected 404 for archived skill, got {resp.status_code}: {resp.json()}"
    )
