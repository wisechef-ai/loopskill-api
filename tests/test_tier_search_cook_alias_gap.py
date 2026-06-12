"""Regression test: search?tier=pro total == snapshot.pro_skills.

Root cause (2026-06-12): marketing_routes.marketing_counts() counts
  pro = by_tier['pro'] + by_tier['cook']   ← includes legacy alias rows
but skill_routes.search_skills() was filtering ONLY on Skill.tier == 'pro',
missing the 7 skills with legacy tier='cook'.  The fix extends the search
filter to include legacy aliases, mirroring the marketing logic.

This test asserts that:
  1. ?tier=pro returns skills whose DB tier is 'pro' OR 'cook'.
  2. ?tier=pro_plus returns skills whose DB tier is 'pro_plus', 'operator', OR 'studio'.
  3. ?tier=free returns ONLY skills whose DB tier is 'free'.
  4. The ?tier=cook legacy alias still works (maps to pro+cook set).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_skill(db, slug: str, tier: str) -> None:
    from app.models import Skill

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug.replace("-", " ").title(),
        description="desc",
        category="devops",
        tier=tier,
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()


@pytest.fixture
def seeded_db(db_session):
    """Seed a mix of canonical + legacy-alias tier rows."""
    _mk_skill(db_session, "free-alpha", "free")
    _mk_skill(db_session, "pro-beta", "pro")          # canonical
    _mk_skill(db_session, "pro-gamma", "pro")          # canonical
    _mk_skill(db_session, "cook-delta", "cook")        # legacy alias for pro
    _mk_skill(db_session, "cook-epsilon", "cook")      # legacy alias for pro
    _mk_skill(db_session, "pro-plus-zeta", "pro_plus")  # canonical
    _mk_skill(db_session, "operator-eta", "operator")  # legacy alias for pro_plus
    _mk_skill(db_session, "studio-theta", "studio")    # legacy alias for pro_plus
    db_session.commit()
    return db_session


# ── tests ─────────────────────────────────────────────────────────────────────


class TestTierSearchAliasGap:
    def test_tier_pro_includes_legacy_cook_rows(self, client, seeded_db):
        """`?tier=pro` must return canonical-pro + legacy-cook skills."""
        resp = client.get("/api/skills/search?tier=pro&page_size=50")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        slugs = {r["slug"] for r in data["results"]}
        # canonical pro rows
        assert "pro-beta" in slugs, "canonical pro row missing from ?tier=pro"
        assert "pro-gamma" in slugs, "canonical pro row missing from ?tier=pro"
        # legacy cook rows — the bug: these used to be silently excluded
        assert "cook-delta" in slugs, "legacy cook row missing from ?tier=pro (alias gap bug)"
        assert "cook-epsilon" in slugs, "legacy cook row missing from ?tier=pro (alias gap bug)"
        # non-pro must be absent
        assert "free-alpha" not in slugs
        assert "pro-plus-zeta" not in slugs
        assert "operator-eta" not in slugs
        assert "studio-theta" not in slugs
        # total must reflect all 4 rows
        assert data["total"] >= 4, f"expected ≥4 pro+cook results, got {data['total']}"

    def test_tier_pro_plus_includes_operator_and_studio(self, client, seeded_db):
        """`?tier=pro_plus` must return canonical + operator + studio rows."""
        resp = client.get("/api/skills/search?tier=pro_plus&page_size=50")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        slugs = {r["slug"] for r in data["results"]}
        assert "pro-plus-zeta" in slugs
        assert "operator-eta" in slugs
        assert "studio-theta" in slugs
        assert "pro-beta" not in slugs
        assert "cook-delta" not in slugs
        assert "free-alpha" not in slugs

    def test_tier_free_returns_only_free(self, client, seeded_db):
        """`?tier=free` must NOT leak pro/cook rows."""
        resp = client.get("/api/skills/search?tier=free&page_size=50")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        slugs = {r["slug"] for r in data["results"]}
        assert "free-alpha" in slugs
        assert "pro-beta" not in slugs
        assert "cook-delta" not in slugs

    def test_legacy_cook_alias_input_returns_pro_and_cook_rows(self, client, seeded_db):
        """`?tier=cook` (legacy input) resolves to canonical 'pro' + legacy 'cook'."""
        resp = client.get("/api/skills/search?tier=cook&page_size=50")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        slugs = {r["slug"] for r in data["results"]}
        # cook input maps to the same expansion as tier=pro
        assert "pro-beta" in slugs
        assert "cook-delta" in slugs
        assert "free-alpha" not in slugs

    def test_snapshot_and_search_pro_count_agree(self, client, seeded_db):
        """snapshot.pro_skills == search?tier=pro total (the invariant that was broken)."""
        snap_resp = client.get("/api/marketing/snapshot")
        assert snap_resp.status_code == 200, snap_resp.text
        snap_pro = snap_resp.json()["counts"]["pro_skills"]

        search_resp = client.get("/api/skills/search?tier=pro&page_size=1")
        assert search_resp.status_code == 200, search_resp.text
        search_total = search_resp.json()["total"]

        assert snap_pro == search_total, (
            f"snapshot.pro_skills={snap_pro} != search?tier=pro total={search_total} "
            "(alias gap regression)"
        )
