"""Tests that GET /api/skills/<slug> respects is_archived + alias rules.

quality_1705 Phase A followup — the original Phase A migration archived
the 3 hard culls + 4 hub-search variants and created skill_aliases for
2 renames + 4 merges, but the GET-by-slug route filter didn't include
is_archived=False, so archived rows kept serving 200 OK instead of
falling through to the alias-or-404 path.

Acceptance gates this guards:
- web-scraper-pro / email-composer / whisper → 404 (archived, no alias)
- incident-response-openclaw → 301 → incident-response (alias hits)
- hub-search-* (4 variants) → 301 → local-skills-discovery (alias hits)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.models import Base
from app.routes import router as api_router


@pytest.fixture()
def db_engine(tmp_path):
    db_path = tmp_path / "archived.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def app_with_db(db_engine):
    SessionLocal = sessionmaker(bind=db_engine, future=True)
    app = FastAPI()
    app.include_router(api_router)

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return app, SessionLocal


@pytest.fixture()
def seeded_app(app_with_db, db_engine):
    app, SessionLocal = app_with_db
    now = datetime.now(timezone.utc)

    # Seed:
    # - one archived skill with NO alias (web-scraper-pro) → expect 404
    # - one archived skill WITH alias to a live skill (hub-search-claude-code → local-skills-discovery)
    # - one live skill (clean-architecture, target of nothing)
    # - one live skill that is the alias target (local-skills-discovery)
    # - a rename pair (incident-response-openclaw → incident-response, both rows exist after rename script)
    rows = [
        # (slug, is_archived, archived_at)
        ("web-scraper-pro", True, now),
        ("hub-search-claude-code", True, now),
        ("clean-architecture", False, None),
        ("local-skills-discovery", False, None),
        ("incident-response", False, None),
    ]
    with SessionLocal() as session:
        with session.begin():
            for slug, archived, archived_at in rows:
                session.execute(
                    text(
                        "INSERT INTO skills (id, slug, title, description, is_public, "
                        "is_archived, archived_at, skill_variant, upstream_status, "
                        "install_count, created_at, updated_at) VALUES "
                        "(:id, :s, :t, :d, 1, :a, :aa, 'custom', 'active', 0, :n, :n)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "s": slug,
                        "t": slug.title(),
                        "d": f"desc for {slug}",
                        "a": 1 if archived else 0,
                        "aa": archived_at,
                        "n": now,
                    },
                )
            # aliases
            session.execute(
                text(
                    "INSERT INTO skill_aliases (old_slug, new_slug, expires_at, created_at) "
                    "VALUES (:o, :n, NULL, :c)"
                ),
                {
                    "o": "hub-search-claude-code",
                    "n": "local-skills-discovery",
                    "c": now,
                },
            )
            session.execute(
                text(
                    "INSERT INTO skill_aliases (old_slug, new_slug, expires_at, created_at) "
                    "VALUES (:o, :n, NULL, :c)"
                ),
                {
                    "o": "incident-response-openclaw",
                    "n": "incident-response",
                    "c": now,
                },
            )
    return app


def test_archived_skill_without_alias_returns_404(seeded_app):
    """web-scraper-pro is archived, has NO alias → 404."""
    client = TestClient(seeded_app)
    resp = client.get("/api/skills/web-scraper-pro", follow_redirects=False)
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


def test_archived_skill_with_alias_returns_301(seeded_app):
    """hub-search-claude-code is archived BUT has an alias → 301 to local-skills-discovery."""
    client = TestClient(seeded_app)
    resp = client.get("/api/skills/hub-search-claude-code", follow_redirects=False)
    assert resp.status_code == 301, f"Expected 301, got {resp.status_code}: {resp.text}"
    assert resp.headers["Location"] == "/api/skills/local-skills-discovery"


def test_renamed_skill_alias_redirects(seeded_app):
    """incident-response-openclaw (no row exists post-rename) → 301 via alias."""
    client = TestClient(seeded_app)
    resp = client.get("/api/skills/incident-response-openclaw", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["Location"] == "/api/skills/incident-response"


def test_canonical_skill_returns_200(seeded_app):
    """clean-architecture is live → 200."""
    client = TestClient(seeded_app)
    resp = client.get("/api/skills/clean-architecture")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "clean-architecture"
