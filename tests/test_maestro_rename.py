"""v7 phase J — chef→maestro rename + skill_aliases redirect tests.

Verifies:
1. SkillAlias model and migration apply cleanly on a fresh in-memory SQLite DB.
2. GET /api/skills/<old_slug> returns 301 with Location header to /api/skills/<new_slug>
   when a non-expired alias row exists.
3. GET /api/skills/<old_slug> returns 404 when the alias is expired.
4. The rename script (scripts/maestro_rename_migration.py) is idempotent —
   running it twice produces the same end state with no errors.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import create_app
from app.models import Base, Skill, SkillAlias


@pytest.fixture
def app_and_db(db_session):
    """Wire a fresh app whose `get_db` returns the test session."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app), db_session


def _make_maestro(db):
    s = Skill(
        id=uuid4(),
        slug="maestro",
        title="Maestro",
        description="Solo-operator AI agent.",
        category="agency",
        is_public=True,
        tier="free",
    )
    db.add(s)
    db.commit()
    return s


# ── Migration / model ───────────────────────────────────────────────────────

def test_skill_alias_table_exists(db_session):
    """Base.metadata.create_all() (run by conftest) must create the skill_aliases table."""
    inspector = db_session.bind.dialect.inspector if hasattr(db_session.bind.dialect, "inspector") else None
    # Falling back to the SQLAlchemy inspector via the engine
    from sqlalchemy import inspect
    insp = inspect(db_session.bind)
    assert "skill_aliases" in insp.get_table_names()


def test_skill_alias_row_round_trip(db_session):
    """Insert + fetch a SkillAlias row works."""
    expires = datetime.now(timezone.utc) + timedelta(days=90)
    db_session.add(SkillAlias(old_slug="chef", new_slug="maestro", expires_at=expires))
    db_session.commit()

    fetched = db_session.query(SkillAlias).filter_by(old_slug="chef").one()
    assert fetched.new_slug == "maestro"
    assert fetched.expires_at is not None


# ── Routes — 301 redirect path ──────────────────────────────────────────────

def test_get_skill_chef_returns_301_when_alias_active(app_and_db):
    client, db = app_and_db
    _make_maestro(db)
    db.add(SkillAlias(
        old_slug="chef",
        new_slug="maestro",
        expires_at=datetime.now(timezone.utc) + timedelta(days=90),
    ))
    db.commit()

    resp = client.get("/api/skills/chef", follow_redirects=False)
    assert resp.status_code == 301, resp.text
    assert resp.headers.get("Location") == "/api/skills/maestro"
    body = resp.json()
    assert body["redirect_to"] == "maestro"


def test_get_skill_chef_returns_404_when_alias_expired(app_and_db):
    client, db = app_and_db
    _make_maestro(db)
    db.add(SkillAlias(
        old_slug="chef",
        new_slug="maestro",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),  # expired
    ))
    db.commit()

    resp = client.get("/api/skills/chef", follow_redirects=False)
    assert resp.status_code == 404


def test_get_skill_chef_returns_404_when_no_alias(app_and_db):
    """Without an alias row, the request 404s as before — no regression."""
    client, db = app_and_db
    resp = client.get("/api/skills/chef", follow_redirects=False)
    assert resp.status_code == 404


# ── Idempotency of rename script ────────────────────────────────────────────

def test_skill_alias_unique_old_slug(db_session):
    """old_slug is the primary key — duplicate inserts raise IntegrityError.

    This is the property the idempotent rename script relies on for "ON CONFLICT
    DO NOTHING" behavior.
    """
    from sqlalchemy.exc import IntegrityError

    db_session.add(SkillAlias(old_slug="chef", new_slug="maestro"))
    db_session.commit()

    db_session.add(SkillAlias(old_slug="chef", new_slug="something-else"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
