"""WIS-948: search ?limit= alias + MCP default cap regression.

Root cause: GET /api/skills/search only exposed `page_size` (default 20,
max 100).  Callers who passed `?limit=200` got the 20-result default silently
because `limit` was not a recognised param.  Pro-tier buyers browsing the
catalog could only see 20 of 63 paid skills they own.

Fixes shipped (2026-06-13):
  1. HTTP endpoint: `limit` Query alias added; when both `limit` and `page_size`
     are supplied, `limit` wins; both are clamped at 100.
  2. MCP tool (recipes_search): default limit raised 20 → 100.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models import Skill


# ── helpers ───────────────────────────────────────────────────────────────────


def _seed_n_skills(db, n: int, tier: str = "pro") -> list[Skill]:
    """Insert n distinct public skills at the given tier."""
    skills = []
    for i in range(n):
        s = Skill(
            id=uuid4(),
            slug=f"wis948-test-{i:04d}",
            title=f"WIS-948 Test Skill {i}",
            description=f"Catalog pagination regression test skill {i}.",
            category="test",
            tier=tier,
            is_public=True,
            is_archived=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(s)
        skills.append(s)
    db.flush()
    return skills


# ── HTTP endpoint tests ────────────────────────────────────────────────────────


class TestSearchLimitAlias:
    """GET /api/skills/search with ?limit= alias."""

    def test_limit_alias_returns_more_than_default(self, client, db_session):
        """Default page_size=50; ?limit= alias must override it upward."""
        _seed_n_skills(db_session, 60)

        # Default (page_size=50) — should return 50 of the 60 seeded skills
        r_default = client.get("/api/skills/search")
        assert r_default.status_code == 200
        default_results = r_default.json()["results"]
        assert len(default_results) == 50, (
            f"expected default page_size=50, got {len(default_results)}"
        )

        # With ?limit=100 — must return all 60 (all seeded) > 50
        r_limit = client.get("/api/skills/search?limit=100")
        assert r_limit.status_code == 200
        limit_results = r_limit.json()["results"]
        assert len(limit_results) >= 60, (
            f"?limit=100 should return >=60 skills, got {len(limit_results)}"
        )

    def test_limit_alias_wins_over_page_size(self, client, db_session):
        """When both ?limit= and ?page_size= are supplied, limit wins."""
        _seed_n_skills(db_session, 40)

        # limit=30 should beat page_size=10
        r = client.get("/api/skills/search?page_size=10&limit=30")
        assert r.status_code == 200
        assert len(r.json()["results"]) >= 30, (
            "limit=30 should override page_size=10"
        )

    def test_limit_alias_capped_at_100_returns_422_not_default(self, client, db_session):
        """?limit= values above 100 must return 422 (explicit rejection), NOT
        silently fall through to the 20-row default (the original bug).

        Before WIS-948: ?limit=200 silently returned 20 rows (limit was unknown).
        After WIS-948:  ?limit=200 returns 422 — honest, explicit cap enforcement.
        """
        _seed_n_skills(db_session, 60)

        r = client.get("/api/skills/search?limit=200")
        assert r.status_code == 422, (
            f"?limit=200 should be rejected with 422 (explicit cap at 100), "
            f"got {r.status_code}"
        )
        # Confirm that ?limit=100 (the valid max) works and returns >20 rows
        r100 = client.get("/api/skills/search?limit=100")
        assert r100.status_code == 200
        assert len(r100.json()["results"]) >= 60, (
            "?limit=100 should return all 60 seeded skills"
        )

    def test_page_size_100_still_works(self, client, db_session):
        """Existing callers using ?page_size=100 are unaffected."""
        _seed_n_skills(db_session, 25)

        r = client.get("/api/skills/search?page_size=100")
        assert r.status_code == 200
        assert len(r.json()["results"]) >= 25

    def test_tier_filter_with_limit_reaches_full_pro_catalog(self, client, db_session):
        """?tier=pro&limit=100 must return >20 pro skills (the original bug)."""
        _seed_n_skills(db_session, 30, tier="pro")

        # Bug: ?tier=pro returned 20 even with limit=200
        r = client.get("/api/skills/search?tier=pro&limit=100")
        assert r.status_code == 200
        data = r.json()
        assert len(data["results"]) >= 30, (
            f"?tier=pro&limit=100 should return >=30 pro skills, got {len(data['results'])}"
        )
        assert data["total"] >= 30


# ── MCP tool tests ─────────────────────────────────────────────────────────────


class TestMcpSearchDefault:
    """recipes_search() MCP tool — default limit raised 20 → 100."""

    def test_default_limit_is_100(self, db_session):
        """Bare recipes_search() without limit arg returns up to 100 results."""
        from app.mcp.tools.search import recipes_search

        _seed_n_skills(db_session, 30)

        result = recipes_search(db_session)
        assert result["total"] >= 30, (
            f"total should be >=30, got {result['total']}"
        )
        assert len(result["results"]) >= 30, (
            f"results length should be >=30 with default limit=100, got {len(result['results'])}"
        )

    def test_mcp_explicit_limit_respected(self, db_session):
        """recipes_search(limit=5) must return at most 5 results."""
        from app.mcp.tools.search import recipes_search

        _seed_n_skills(db_session, 10)

        result = recipes_search(db_session, limit=5)
        assert len(result["results"]) <= 5
