"""Tests for the cookbook→bundle well-known bridge (app/bundle_wellknown_routes.py).

Verifies the agentskills.io discovery surface a public cookbook exposes:
  - index.json lists every skill (free + paid), paid flagged `locked`
  - FREE skill SKILL.md serves the real readme body
  - PAID skill SKILL.md serves a non-leaking stub (no readme body crosses)
  - private cookbook → 404
  - skill not in cookbook → 404
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

from app.database import get_db
from app.models import Base, Cookbook, CookbookSkill, Skill, User

PAID_BODY = "SECRET PAID INSTRUCTIONS — must never leak over well-known"
FREE_BODY = "# Free Skill\n\nReal public body, safe to serve."


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


def _app(db: Session) -> FastAPI:
    from app.bundle_wellknown_routes import router as wk_router

    app = FastAPI()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(wk_router)
    return app


def _seed_public_cookbook(db: Session, *, visibility: str = "public") -> Cookbook:
    owner = User(id=uuid4(), display_name="O", email=f"{uuid4()}@t.example")
    db.add(owner)
    db.flush()

    free = Skill(
        id=uuid4(),
        slug="free-skill",
        title="Free Skill",
        description="A free one",
        tier="free",
        is_public=True,
        readme=FREE_BODY,
    )
    paid = Skill(
        id=uuid4(),
        slug="paid-skill",
        title="Paid Skill",
        description="A paid one",
        tier="pro",
        is_public=True,
        readme=PAID_BODY,
    )
    db.add_all([free, paid])
    db.flush()

    cb = Cookbook(
        id=uuid4(),
        name="Test Bundle",
        slug="test-bundle",
        visibility=visibility,
        bundle_owner=owner.id,
    )
    db.add(cb)
    db.flush()
    db.add_all(
        [
            CookbookSkill(bundle_id=cb.id, skill_id=free.id, source="custom-added", install_order=0),
            CookbookSkill(bundle_id=cb.id, skill_id=paid.id, source="custom-added", install_order=1),
        ]
    )
    db.commit()
    return cb


class TestWellKnownIndex:
    def test_index_lists_all_skills_free_and_paid(self, db_session):
        _seed_public_cookbook(db_session)
        with TestClient(_app(db_session)) as c:
            r = c.get("/api/cookbooks/public/test-bundle/.well-known/skills/index.json")
        assert r.status_code == 200, r.text
        body = r.json()
        names = {s["name"] for s in body["skills"]}
        assert names == {"free-skill", "paid-skill"}, "index must list the WHOLE bundle"
        # agentskills.io shape: each entry has name/description/files
        for s in body["skills"]:
            assert s["files"] == ["SKILL.md"]
            assert s["description"]
        # paid flagged locked, free not
        by = {s["name"]: s for s in body["skills"]}
        assert by["paid-skill"].get("locked") is True
        assert "locked" not in by["free-skill"]
        assert body["cookbook"]["skill_count"] == 2

    def test_private_cookbook_index_404(self, db_session):
        _seed_public_cookbook(db_session, visibility="private")
        with TestClient(_app(db_session)) as c:
            r = c.get("/api/cookbooks/public/test-bundle/.well-known/skills/index.json")
        assert r.status_code == 404


class TestWellKnownSkillMd:
    def test_free_skill_serves_real_body(self, db_session):
        _seed_public_cookbook(db_session)
        with TestClient(_app(db_session)) as c:
            r = c.get("/api/cookbooks/public/test-bundle/.well-known/skills/free-skill/SKILL.md")
        assert r.status_code == 200, r.text
        assert FREE_BODY in r.text
        assert r.headers["content-type"].startswith("text/markdown")

    def test_paid_skill_serves_stub_not_body(self, db_session):
        _seed_public_cookbook(db_session)
        with TestClient(_app(db_session)) as c:
            r = c.get("/api/cookbooks/public/test-bundle/.well-known/skills/paid-skill/SKILL.md")
        assert r.status_code == 200, r.text
        # PAYWALL INVARIANT: the real paid body must NOT appear.
        assert PAID_BODY not in r.text
        # The stub is a valid skill the agent can register + a clear pointer.
        assert "name: paid-skill" in r.text
        assert "locked: true" in r.text
        assert "recipes_cookbook_install" in r.text

    def test_skill_not_in_cookbook_404(self, db_session):
        _seed_public_cookbook(db_session)
        with TestClient(_app(db_session)) as c:
            r = c.get("/api/cookbooks/public/test-bundle/.well-known/skills/nope/SKILL.md")
        assert r.status_code == 404
