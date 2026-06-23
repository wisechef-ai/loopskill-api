"""Tests for skill `related_skills` surface (Stage 1, WIS-694).

Covers DB persistence, API response shape, and edge cases:
- internal/non-public skills filtered out
- dangling slugs dropped silently
- self-references filtered
- response capped at 10
- 404 on unknown slug
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import make_skill


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def two_linked_skills(db_session: Session):
    """`a` lists `b` in related_skills. Both public."""
    a = make_skill(db_session, slug="skill-a", title="Skill A", related_skills=["skill-b"])
    b = make_skill(db_session, slug="skill-b", title="Skill B")
    db_session.commit()
    return a, b


@pytest.fixture
def public_skill_with_related(db_session: Session):
    a = make_skill(db_session, slug="alpha", title="Alpha", related_skills=["beta", "gamma"])
    make_skill(db_session, slug="beta", title="Beta")
    make_skill(db_session, slug="gamma", title="Gamma")
    db_session.commit()
    return a


@pytest.fixture
def public_skill_linking_internal(db_session: Session):
    """A public skill that lists an internal (non-public) skill — must filter out."""
    a = make_skill(
        db_session, slug="public-x", title="Public X",
        related_skills=["internal-y", "public-z"],
    )
    make_skill(db_session, slug="internal-y", title="Internal Y", is_public=False)
    make_skill(db_session, slug="public-z", title="Public Z")
    db_session.commit()
    return a


@pytest.fixture
def skill_with_bad_link(db_session: Session):
    """Lists a slug that doesn't exist in DB — must be silently dropped."""
    a = make_skill(
        db_session, slug="orphan-host", title="Orphan Host",
        related_skills=["does-not-exist", "real-target"],
    )
    make_skill(db_session, slug="real-target", title="Real Target")
    db_session.commit()
    return a


@pytest.fixture
def self_referencing_skill(db_session: Session):
    """Lists itself + a real link. Self must filter out, real must remain."""
    a = make_skill(
        db_session, slug="navel", title="Navel",
        related_skills=["navel", "outward"],
    )
    make_skill(db_session, slug="outward", title="Outward")
    db_session.commit()
    return a


@pytest.fixture
def skill_with_15_related(db_session: Session):
    """15 declared related skills — endpoint must cap at 10."""
    targets = [f"target-{i:02d}" for i in range(15)]
    a = make_skill(
        db_session, slug="popular", title="Popular",
        related_skills=targets,
    )
    for t in targets:
        make_skill(db_session, slug=t, title=t.title())
    db_session.commit()
    return a


# ── Tests ───────────────────────────────────────────────────────────────────

class TestRelatedSkillsPersistence:
    def test_skill_can_have_related_skills_persisted(self, db_session: Session):
        """SKILL frontmatter related_skills round-trips through DB."""
        from app.models import Skill
        s = make_skill(
            db_session, slug="round-trip", title="Round Trip",
            related_skills=["a", "b", "c"],
        )
        db_session.commit()

        fetched = db_session.query(Skill).filter_by(slug="round-trip").one()
        assert fetched.related_skills == ["a", "b", "c"]

    def test_skill_without_related_defaults_to_empty(self, db_session: Session):
        """A skill with no related_skills column set is `[]` or `None` — never errors."""
        from app.models import Skill
        s = make_skill(db_session, slug="lone", title="Lone")
        db_session.commit()
        fetched = db_session.query(Skill).filter_by(slug="lone").one()
        assert fetched.related_skills in (None, [], {})


class TestRelatedSkillsAPI:
    def test_get_skill_detail_includes_related(self, client: TestClient, two_linked_skills):
        """GET /api/skills/{slug} returns related as resolved objects, not raw slugs."""
        r = client.get("/api/skills/skill-a")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "related" in body, "SkillDetailOut should expose `related` field"
        assert isinstance(body["related"], list)
        assert len(body["related"]) == 1
        # Resolved, not raw slug — must be a dict-shaped SkillOut
        assert body["related"][0]["slug"] == "skill-b"
        assert body["related"][0]["title"] == "Skill B"

    def test_get_skill_related_endpoint_public(self, client: TestClient, public_skill_with_related):
        """GET /api/skills/{slug}/related works without auth, returns up to 10."""
        r = client.get("/api/skills/alpha/related")
        assert r.status_code == 200, r.text
        body = r.json()
        # Endpoint returns a list directly OR {"related": [...]} — accept either canonical shape
        items = body if isinstance(body, list) else body.get("related", body)
        assert isinstance(items, list)
        slugs = sorted(item["slug"] for item in items)
        assert slugs == ["beta", "gamma"]

    def test_related_excludes_internal_skills(self, client: TestClient, public_skill_linking_internal):
        """Internal/non-public skills must not appear in /related responses."""
        r = client.get("/api/skills/public-x/related")
        assert r.status_code == 200
        items = r.json() if isinstance(r.json(), list) else r.json().get("related", [])
        slugs = [item["slug"] for item in items]
        assert "internal-y" not in slugs
        assert "public-z" in slugs

    def test_related_handles_dangling_slug_gracefully(self, client: TestClient, skill_with_bad_link):
        """A related_skills entry pointing to a non-existent slug is silently dropped."""
        r = client.get("/api/skills/orphan-host/related")
        assert r.status_code == 200
        items = r.json() if isinstance(r.json(), list) else r.json().get("related", [])
        slugs = [item["slug"] for item in items]
        assert "does-not-exist" not in slugs
        assert "real-target" in slugs
        # No error, no 500, no leaked None entries
        assert all(item.get("slug") for item in items)

    def test_related_endpoint_404s_for_unknown_slug(self, client: TestClient):
        """GET /api/skills/nonexistent/related → 404 with consistent error shape."""
        r = client.get("/api/skills/totally-not-a-skill/related")
        assert r.status_code == 404
        body = r.json()
        assert "detail" in body

    def test_related_handles_self_reference(self, client: TestClient, self_referencing_skill):
        """A skill that lists itself in related_skills filters self out."""
        r = client.get("/api/skills/navel/related")
        assert r.status_code == 200
        items = r.json() if isinstance(r.json(), list) else r.json().get("related", [])
        slugs = [item["slug"] for item in items]
        assert "navel" not in slugs
        assert "outward" in slugs

    def test_related_caps_at_ten(self, client: TestClient, skill_with_15_related):
        """Server caps response at 10 even if more are declared."""
        r = client.get("/api/skills/popular/related")
        assert r.status_code == 200
        items = r.json() if isinstance(r.json(), list) else r.json().get("related", [])
        assert len(items) == 10
