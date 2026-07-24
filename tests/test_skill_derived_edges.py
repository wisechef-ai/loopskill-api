"""Tests for skill graph Stage 2 (G16, WIS-695) — derived edges.

Three signals combine into a single edge weight:
  A) Jaccard similarity of tag sets (parsed from latest skill_toml)
  B) Same-category co-occurrence
  C) Co-install score (same api_key installs both within 30d)

Weight = 0.6 * jaccard + 0.2 * category_match + 0.2 * coinstall_score
Edges below `WEIGHT_THRESHOLD` are dropped. Per-skill top-K cap = 10.

Surfaces:
  - skill_derived_edges table (source_slug, target_slug, weight, signals JSON, last_built_at)
  - app.edge_builder.build_edges(db) -> list[dict] (pure)
  - app.edge_builder.persist_edges(db, edges) -> int (replaces existing)
  - GET /api/skills/{slug}/graph -> {declared: [...], derived: [...], all: [...top10...]}
  - GET /api/stats now includes trending_pairs: [{a, b, weight}, ...] top 10
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import make_skill


# ── Helpers ─────────────────────────────────────────────────────────────

def make_skill_with_tags(db: Session, slug: str, tags: list[str], **kw):
    """Create a Skill plus a SkillVersion whose skill_toml encodes the given tags."""
    from app.models import SkillVersion
    skill = make_skill(db, slug=slug, title=slug.title(), **kw)
    toml_text = (
        "[skill]\n"
        f'name = "{slug}"\n'
        f'tags = {json.dumps(tags)}\n'
    )
    v = SkillVersion(
        id=uuid4(), skill_id=skill.id, semver="1.0.0", skill_toml=toml_text,
    )
    db.add(v)
    db.flush()
    return skill


def make_install(db, skill, api_key_id, days_ago: int = 0):
    from app.models import InstallEvent
    from datetime import datetime, timezone, timedelta
    e = InstallEvent(
        id=uuid4(),
        skill_id=skill.id,
        skill_slug=skill.slug,
        api_key_id=api_key_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(e)
    db.flush()
    return e


# ── 1. Jaccard signal ───────────────────────────────────────────────────

class TestJaccardSignal:
    def test_jaccard_half_when_two_of_four_tags_overlap(self):
        from app.edge_builder import jaccard
        assert jaccard({"a", "b"}, {"a", "c", "d"}) == pytest.approx(1 / 4)
        assert jaccard({"a", "b", "c"}, {"a", "b", "d", "e"}) == pytest.approx(2 / 5)

    def test_jaccard_zero_when_no_overlap(self):
        from app.edge_builder import jaccard
        assert jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_jaccard_one_when_identical(self):
        from app.edge_builder import jaccard
        assert jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_jaccard_zero_on_empty_either_side(self):
        from app.edge_builder import jaccard
        assert jaccard(set(), {"a"}) == 0.0
        assert jaccard({"a"}, set()) == 0.0
        assert jaccard(set(), set()) == 0.0


# ── 2. Tag extraction from skill_toml ───────────────────────────────────

class TestTagExtraction:
    def test_extracts_tags_from_latest_skill_toml(self, db_session: Session):
        from app.edge_builder import extract_tags
        s = make_skill_with_tags(db_session, "alpha", ["devops", "cron", "watchdog"])
        db_session.commit()
        assert set(extract_tags(s)) == {"devops", "cron", "watchdog"}

    def test_extracts_empty_when_no_versions(self, db_session: Session):
        from app.edge_builder import extract_tags
        s = make_skill(db_session, slug="naked", title="Naked")
        db_session.commit()
        assert extract_tags(s) == []

    def test_extracts_empty_when_toml_has_no_tags_key(self, db_session: Session):
        from app.edge_builder import extract_tags
        from app.models import SkillVersion
        s = make_skill(db_session, slug="tagless", title="Tagless")
        db_session.add(SkillVersion(
            id=uuid4(), skill_id=s.id, semver="1.0.0",
            skill_toml='[skill]\nname = "tagless"\n',
        ))
        db_session.commit()
        assert extract_tags(s) == []


# ── 3. build_edges produces correctly weighted records ──────────────────

class TestBuildEdges:
    def test_build_edges_finds_tag_overlap_pair(self, db_session: Session):
        from app.edge_builder import build_edges
        make_skill_with_tags(db_session, "a", ["docker", "ci", "deploy"], category="devops")
        make_skill_with_tags(db_session, "b", ["docker", "ci", "monitoring"], category="devops")
        make_skill_with_tags(db_session, "c", ["unrelated"], category="creative")
        db_session.commit()

        edges = build_edges(db_session)
        # Must contain a<->b pair (both directions expected for query convenience)
        pair_set = {(e["source_slug"], e["target_slug"]) for e in edges}
        assert ("a", "b") in pair_set
        assert ("b", "a") in pair_set

    def test_build_edges_skips_self_loops(self, db_session: Session):
        from app.edge_builder import build_edges
        make_skill_with_tags(db_session, "a", ["x", "y"], category="devops")
        make_skill_with_tags(db_session, "b", ["x", "y"], category="devops")
        db_session.commit()
        edges = build_edges(db_session)
        for e in edges:
            assert e["source_slug"] != e["target_slug"], "no self-edges"

    def test_build_edges_skips_internal_skills(self, db_session: Session):
        from app.edge_builder import build_edges
        make_skill_with_tags(db_session, "pub", ["x", "y"], category="devops")
        make_skill_with_tags(db_session, "internal", ["x", "y"], category="devops", is_public=False)
        db_session.commit()
        edges = build_edges(db_session)
        slugs = {e["source_slug"] for e in edges} | {e["target_slug"] for e in edges}
        assert "internal" not in slugs

    def test_build_edges_drops_below_threshold(self, db_session: Session):
        from app.edge_builder import build_edges, WEIGHT_THRESHOLD
        # Tiny overlap (1/10 = 0.1 jaccard, no category match, no co-install)
        # Combined = 0.6 * 0.1 = 0.06 — below default 0.15 threshold
        make_skill_with_tags(db_session, "a",
            ["x", "a1", "a2", "a3", "a4"], category="devops")
        make_skill_with_tags(db_session, "b",
            ["x", "b1", "b2", "b3", "b4"], category="creative")
        db_session.commit()
        edges = build_edges(db_session)
        pair_set = {(e["source_slug"], e["target_slug"]) for e in edges}
        # 1/9 jaccard * 0.6 = 0.067 → below 0.15 threshold
        assert WEIGHT_THRESHOLD >= 0.15
        assert ("a", "b") not in pair_set

    def test_build_edges_includes_signal_breakdown(self, db_session: Session):
        from app.edge_builder import build_edges
        make_skill_with_tags(db_session, "a", ["x", "y"], category="devops")
        make_skill_with_tags(db_session, "b", ["x", "y"], category="devops")
        db_session.commit()
        edges = [e for e in build_edges(db_session)
                 if e["source_slug"] == "a" and e["target_slug"] == "b"]
        assert edges, "expected a→b edge"
        e = edges[0]
        assert "signals" in e
        assert "jaccard" in e["signals"]
        assert "category" in e["signals"]
        assert "coinstall" in e["signals"]
        assert e["signals"]["jaccard"] == pytest.approx(1.0)
        assert e["signals"]["category"] == 1.0  # same category


# ── 4. persist_edges writes derived_edges table ─────────────────────────

class TestPersistEdges:
    def test_persist_edges_writes_rows(self, db_session: Session):
        from app.edge_builder import persist_edges
        from app.models import SkillDerivedEdge
        edges = [
            {"source_slug": "a", "target_slug": "b", "weight": 0.8,
             "signals": {"jaccard": 1.0, "category": 1.0, "coinstall": 0.0}},
            {"source_slug": "b", "target_slug": "a", "weight": 0.8,
             "signals": {"jaccard": 1.0, "category": 1.0, "coinstall": 0.0}},
        ]
        n = persist_edges(db_session, edges)
        db_session.commit()
        assert n == 2
        rows = db_session.query(SkillDerivedEdge).all()
        assert len(rows) == 2

    def test_persist_edges_replaces_existing(self, db_session: Session):
        """Re-running the builder must idempotently replace, not duplicate."""
        from app.edge_builder import persist_edges
        from app.models import SkillDerivedEdge
        e1 = [{"source_slug": "a", "target_slug": "b", "weight": 0.5, "signals": {}}]
        e2 = [{"source_slug": "a", "target_slug": "b", "weight": 0.9, "signals": {}}]
        persist_edges(db_session, e1)
        db_session.commit()
        persist_edges(db_session, e2)
        db_session.commit()
        rows = db_session.query(SkillDerivedEdge).all()
        assert len(rows) == 1
        assert rows[0].weight == pytest.approx(0.9)


# ── 5. GET /api/skills/{slug}/graph ─────────────────────────────────────

class TestGraphEndpoint:
    def test_graph_returns_declared_and_derived(self, client: TestClient, db_session: Session):
        # Set up: a declares b. a and c share tags. d unrelated.
        a = make_skill_with_tags(db_session, "a", ["docker", "ci"], category="devops",
                                  related_skills=["b"])
        make_skill_with_tags(db_session, "b", ["other"], category="content")
        make_skill_with_tags(db_session, "c", ["docker", "ci"], category="devops")
        make_skill_with_tags(db_session, "d", ["unrelated"], category="x")
        db_session.commit()

        # Build edges
        from app.edge_builder import build_edges, persist_edges
        edges = build_edges(db_session)
        persist_edges(db_session, edges)
        db_session.commit()

        r = client.get("/api/skills/a/graph")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "declared" in body
        assert "derived" in body
        assert "all" in body
        declared_slugs = [s["slug"] for s in body["declared"]]
        derived_slugs = [s["slug"] for s in body["derived"]]
        assert "b" in declared_slugs
        assert "c" in derived_slugs
        assert "d" not in derived_slugs

    def test_graph_caps_at_10(self, client: TestClient, db_session: Session):
        # 12 skills all share the exact same tag set with 'hub'
        make_skill_with_tags(db_session, "hub", ["x", "y", "z"], category="devops")
        for i in range(12):
            make_skill_with_tags(db_session, f"spoke{i:02d}", ["x", "y", "z"], category="devops")
        db_session.commit()

        from app.edge_builder import build_edges, persist_edges
        persist_edges(db_session, build_edges(db_session))
        db_session.commit()

        r = client.get("/api/skills/hub/graph")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["all"]) <= 10
        assert len(body["derived"]) <= 10

    def test_graph_404_on_unknown_slug(self, client: TestClient):
        r = client.get("/api/skills/does-not-exist/graph")
        assert r.status_code == 404


# ── 6. /api/stats trending_pairs ────────────────────────────────────────

class TestTrendingPairsInStats:
    def test_stats_returns_trending_pairs(self, client: TestClient, db_session: Session):
        make_skill_with_tags(db_session, "a", ["x", "y"], category="devops")
        make_skill_with_tags(db_session, "b", ["x", "y"], category="devops")
        make_skill_with_tags(db_session, "c", ["x"], category="devops")
        db_session.commit()
        from app.edge_builder import build_edges, persist_edges
        persist_edges(db_session, build_edges(db_session))
        db_session.commit()

        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert "trending_pairs" in body
        assert isinstance(body["trending_pairs"], list)

    def test_stats_trending_pairs_dedup_undirected_and_sorted(
        self, client: TestClient, db_session: Session
    ):
        make_skill_with_tags(db_session, "a", ["x", "y", "z"], category="devops")
        make_skill_with_tags(db_session, "b", ["x", "y", "z"], category="devops")  # weight ~ 1.0
        make_skill_with_tags(db_session, "c", ["x", "y"], category="creative")     # lower
        db_session.commit()
        from app.edge_builder import build_edges, persist_edges
        persist_edges(db_session, build_edges(db_session))
        db_session.commit()

        r = client.get("/api/stats")
        body = r.json()
        pairs = body["trending_pairs"]

        # Undirected: each pair should appear at most once
        seen = set()
        for p in pairs:
            key = tuple(sorted([p["a"], p["b"]]))
            assert key not in seen, f"duplicate pair {key}"
            seen.add(key)

        # Sorted by weight desc
        weights = [p["weight"] for p in pairs]
        assert weights == sorted(weights, reverse=True)

        # a-b is the strongest (full tag overlap + same category)
        if pairs:
            top = tuple(sorted([pairs[0]["a"], pairs[0]["b"]]))
            assert top == ("a", "b")
