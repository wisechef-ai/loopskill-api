"""tests/test_v6_subset_filter.py

Tests for v6 Phase A API extensions:
  - GET /api/skills/search?subset=pantry|menu|cookbook&variant=original|custom
  - GET /api/skills/<slug>  — returns skill_variant, original_source_url,
                              external_resources, pinned_sha, upstream_status
  - GET /api/skills/<slug>/external — public no-auth, returns external_resources
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Skill


# ── Test app setup ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine_v6():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def seeded_db(engine_v6):
    """Seed DB with pantry, menu, and custom skills for filter tests."""
    Session_ = sessionmaker(bind=engine_v6)
    session = Session_()
    from datetime import datetime, timezone

    skills = [
        Skill(id=uuid4(), slug="pantry-alpha", title="Pantry Alpha",
              is_public=True, skill_variant="original",
              original_source_url="https://github.com/obra/superpowers",
              pinned_sha="abc123def456" * 2 + "abcd",
              upstream_status="active",
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc)),
        Skill(id=uuid4(), slug="pantry-beta", title="Pantry Beta",
              is_public=True, skill_variant="original",
              original_source_url="https://github.com/Houseofmvps/ultraship",
              pinned_sha="deadbeef" * 8,
              upstream_status="active",
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc)),
        Skill(id=uuid4(), slug="menu-alpha", title="Menu Alpha",
              is_public=True, skill_variant="custom",
              upstream_status="active",
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc)),
        Skill(id=uuid4(), slug="menu-beta", title="Menu Beta",
              is_public=True, skill_variant="custom",
              upstream_status="active",
              external_resources=[
                  {"slug": "ext-tool", "url": "https://ext.example.com",
                   "relation": "complementary", "description": "Pair with this"}
              ],
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc)),
        Skill(id=uuid4(), slug="custom-private", title="Custom Private",
              is_public=False, skill_variant="custom",
              upstream_status="active",
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc)),
        Skill(id=uuid4(), slug="abandoned-skill", title="Abandoned",
              is_public=True, skill_variant="original",
              original_source_url="https://github.com/defunct/repo",
              upstream_status="abandoned",
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc)),
    ]
    session.add_all(skills)
    session.commit()
    session.close()
    return engine_v6


@pytest.fixture(scope="module")
def _v6_monkeypatch():
    """Module-scoped MonkeyPatch — the default `monkeypatch` is function-scoped
    and cannot be consumed by the module-scoped `client_v6` fixture.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        yield mp
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def client_v6(seeded_db, _v6_monkeypatch):
    """Production-wired test client (shared builder).

    Uses build_test_app so APIKeyMiddleware is mounted — the v6
    external_resources field is paywalled and only resolves for a paid /
    master caller, which requires the middleware to stamp auth_ctx from the
    x-api-key header. The legacy hand-mounted app omitted the middleware, so
    auth_ctx was never set and external_resources came back null.
    """
    from app.config import settings
    from tests._app_factory import build_test_app

    Session_ = sessionmaker(bind=seeded_db)
    session = Session_()
    app = build_test_app(db_session=session, monkeypatch=_v6_monkeypatch)
    try:
        with TestClient(app, headers={"x-api-key": settings.API_KEY}) as c:
            yield c
    finally:
        session.close()


# ── subset=pantry filter ─────────────────────────────────────────────────

class TestSubsetPantry:
    def test_pantry_returns_only_original_variant(self, client_v6):
        r = client_v6.get("/api/skills/search?subset=pantry")
        assert r.status_code == 200
        data = r.json()
        slugs = [s["slug"] for s in data["results"]]
        assert "pantry-alpha" in slugs
        assert "pantry-beta" in slugs
        assert "menu-alpha" not in slugs

    def test_pantry_excludes_custom_skills(self, client_v6):
        r = client_v6.get("/api/skills/search?subset=pantry")
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "menu-alpha" not in slugs
        assert "menu-beta" not in slugs

    def test_pantry_excludes_private(self, client_v6):
        r = client_v6.get("/api/skills/search?subset=pantry")
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "custom-private" not in slugs


# ── subset=menu filter ───────────────────────────────────────────────────

class TestSubsetMenu:
    def test_menu_returns_only_custom_variant(self, client_v6):
        r = client_v6.get("/api/skills/search?subset=menu")
        assert r.status_code == 200
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "menu-alpha" in slugs
        assert "menu-beta" in slugs
        assert "pantry-alpha" not in slugs

    def test_menu_excludes_private(self, client_v6):
        r = client_v6.get("/api/skills/search?subset=menu")
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "custom-private" not in slugs


# ── variant filter ───────────────────────────────────────────────────────

class TestVariantFilter:
    def test_variant_original_filter(self, client_v6):
        r = client_v6.get("/api/skills/search?variant=original")
        assert r.status_code == 200
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "pantry-alpha" in slugs
        assert "menu-alpha" not in slugs

    def test_variant_custom_filter(self, client_v6):
        r = client_v6.get("/api/skills/search?variant=custom")
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "menu-alpha" in slugs
        assert "pantry-alpha" not in slugs

    def test_variant_combined_with_q(self, client_v6):
        r = client_v6.get("/api/skills/search?variant=original&q=Beta")
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "pantry-beta" in slugs
        assert "menu-beta" not in slugs


# ── GET /api/skills/<slug> — new v6 fields ───────────────────────────────

class TestSkillDetailV6Fields:
    def test_skill_detail_returns_skill_variant(self, client_v6):
        r = client_v6.get("/api/skills/pantry-alpha")
        assert r.status_code == 200
        data = r.json()
        assert "skill_variant" in data
        assert data["skill_variant"] == "original"

    def test_skill_detail_returns_original_source_url(self, client_v6):
        r = client_v6.get("/api/skills/pantry-alpha")
        data = r.json()
        assert "original_source_url" in data
        assert data["original_source_url"] == "https://github.com/obra/superpowers"

    def test_skill_detail_returns_pinned_sha(self, client_v6):
        r = client_v6.get("/api/skills/pantry-alpha")
        data = r.json()
        assert "pinned_sha" in data
        assert data["pinned_sha"] is not None

    def test_skill_detail_returns_upstream_status(self, client_v6):
        r = client_v6.get("/api/skills/pantry-alpha")
        data = r.json()
        assert "upstream_status" in data
        assert data["upstream_status"] == "active"

    def test_skill_detail_returns_external_resources(self, client_v6):
        r = client_v6.get("/api/skills/menu-beta")
        data = r.json()
        assert "external_resources" in data
        assert isinstance(data["external_resources"], list)
        assert len(data["external_resources"]) == 1
        assert data["external_resources"][0]["slug"] == "ext-tool"

    def test_skill_detail_external_resources_null_for_no_resources(self, client_v6):
        r = client_v6.get("/api/skills/menu-alpha")
        data = r.json()
        assert "external_resources" in data
        assert data["external_resources"] is None or data["external_resources"] == []

    def test_skill_detail_404_still_works(self, client_v6):
        r = client_v6.get("/api/skills/nonexistent-slug")
        assert r.status_code == 404


# ── GET /api/skills/<slug>/external ──────────────────────────────────────

class TestSkillExternalEndpoint:
    def test_external_returns_200(self, client_v6):
        r = client_v6.get("/api/skills/menu-beta/external")
        assert r.status_code == 200

    def test_external_returns_list(self, client_v6):
        r = client_v6.get("/api/skills/menu-beta/external")
        data = r.json()
        assert isinstance(data, list)

    def test_external_returns_correct_resources(self, client_v6):
        r = client_v6.get("/api/skills/menu-beta/external")
        data = r.json()
        assert len(data) == 1
        assert data[0]["slug"] == "ext-tool"
        assert data[0]["relation"] == "complementary"

    def test_external_empty_list_for_no_resources(self, client_v6):
        r = client_v6.get("/api/skills/menu-alpha/external")
        assert r.status_code == 200
        data = r.json()
        assert data == []

    def test_external_404_for_missing_skill(self, client_v6):
        r = client_v6.get("/api/skills/no-such-skill/external")
        assert r.status_code == 404

    def test_external_works_without_auth(self, client_v6):
        # The /external endpoint is in PUBLIC_PREFIXES — no auth required.
        # client_v6 already has no auth header set, so a plain GET works.
        r = client_v6.get("/api/skills/menu-beta/external")
        assert r.status_code == 200


# ── upstream_status filter ───────────────────────────────────────────────

class TestUpstreamStatusFilter:
    def test_abandoned_skill_visible_in_search(self, client_v6):
        r = client_v6.get("/api/skills/search?variant=original")
        slugs = [s["slug"] for s in r.json()["results"]]
        assert "abandoned-skill" in slugs

    def test_abandoned_skill_shows_status_in_detail(self, client_v6):
        r = client_v6.get("/api/skills/abandoned-skill")
        assert r.status_code == 200
        assert r.json()["upstream_status"] == "abandoned"
