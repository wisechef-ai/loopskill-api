"""Tests for Phase B.5 — graph extension to 6 edge types.

The Stage 1-3 graph (G15-G17) shipped 3 edge types: declared `related_skills`,
derived `tag_overlap`, derived `co_install`. B.5 adds three more:

  - `failed_after`        — A run, B run within 5min, B failed
  - `arch_compatible_with`— same-host-fingerprint co-occurrence
  - `replaced_by`         — manual curator + auto-detected candidates

Plus a public `/api/graph/related?skill=&edge=&min_weight=` endpoint.

Tests cover:
  * failed_after derivation with mocked incident_reports rows
  * arch_compatible_with empty path (no host_fingerprint column) and
    populated path (column simulated via raw SQL)
  * replaced_by manual + auto-detection sweep
  * endpoint param validation + public access (no API key required)
  * existing edge types (tag_overlap, co_install, related_skills,
    category_sibling) still answerable via the new endpoint
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.models import (
    Base,
    InstallEvent,
    ReplacementCandidate,
    Skill,
    SkillDerivedEdge,
    SkillReplacement,
    SkillVersion,
)
from tests.conftest import make_skill


# ── Test scaffolding ────────────────────────────────────────────────────

@pytest.fixture()
def graph_app(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with the graph router + middleware-equivalent.

    The standard `client` fixture only mounts the core router. We need
    `app.graph_routes` mounted here AND we want to bypass the API-key
    middleware (since the prefix is public per the production config).
    """
    from app.graph_routes import router as graph_router

    app = FastAPI()
    app.include_router(graph_router)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _make_skill_with_tags(db: Session, slug: str, tags: list[str], **kw) -> Skill:
    skill = make_skill(db, slug=slug, title=slug.title(), **kw)
    toml_text = (
        "[skill]\n"
        f'name = "{slug}"\n'
        f'tags = {json.dumps(tags)}\n'
    )
    db.add(SkillVersion(
        id=uuid4(), skill_id=skill.id, semver="1.0.0", skill_toml=toml_text,
    ))
    db.flush()
    return skill


def _ensure_incident_reports_table(db: Session) -> None:
    """Create the test-shape incident_reports table.

    The sibling B.1 task ships a different schema (skill_id FK, error_signature,
    env_fingerprint, agent_fp_anon, ...). For graph-extension derivation tests
    we use a slug+signature shape, so drop any existing table and recreate.
    """
    db.execute(text("DROP TABLE IF EXISTS incident_reports"))
    db.execute(text(
        """
        CREATE TABLE incident_reports (
            id VARCHAR(36) PRIMARY KEY,
            skill_slug VARCHAR(255),
            signature VARCHAR(255),
            occurred_at DATETIME
        )
        """
    ))
    db.flush()


def _drop_incident_reports_table(db: Session) -> None:
    db.execute(text("DROP TABLE IF EXISTS incident_reports"))
    db.flush()


def _insert_incident(db: Session, skill_slug: str, signature: str, when: datetime) -> None:
    db.execute(
        text(
            "INSERT INTO incident_reports (id, skill_slug, signature, occurred_at) "
            "VALUES (:id, :slug, :sig, :ts)"
        ),
        {"id": str(uuid4()), "slug": skill_slug, "sig": signature, "ts": when},
    )
    db.flush()


# ── 1. failed_after derivation ──────────────────────────────────────────

class TestFailedAfter:
    def test_returns_empty_when_table_missing(self, db_session: Session):
        from app.graph_extension import failed_after_edges

        # Make sure the table is gone (the migration adds skill_replacements,
        # but `incident_reports` is owned by the sibling B.1 task).
        _drop_incident_reports_table(db_session)
        _make_skill_with_tags(db_session, "victim", ["x"])
        db_session.commit()

        edges = failed_after_edges(db_session, "victim")
        assert edges == []

    def test_derives_from_recent_install_then_incident(self, db_session: Session):
        from app.graph_extension import failed_after_edges

        _ensure_incident_reports_table(db_session)
        try:
            preceder = _make_skill_with_tags(db_session, "preceder", ["x"])
            victim = _make_skill_with_tags(db_session, "victim", ["y"])
            db_session.commit()

            t0 = datetime.now(timezone.utc)
            # Predecessor installed 2 minutes before victim's incident
            db_session.add(InstallEvent(
                id=uuid4(),
                skill_id=preceder.id,
                skill_slug=preceder.slug,
                created_at=t0 - timedelta(minutes=2),
            ))
            db_session.commit()

            _insert_incident(db_session, "victim", "TimeoutError", t0)
            _insert_incident(db_session, "victim", "TimeoutError", t0 + timedelta(minutes=10))

            edges = failed_after_edges(db_session, "victim")
            assert len(edges) == 1
            e = edges[0]
            assert e["skill_slug"] == "preceder"
            assert e["edge_type"] == "failed_after"
            # 1 hit / 2 incidents
            assert e["weight"] == pytest.approx(0.5)
            assert e["evidence_count"] == 1
        finally:
            _drop_incident_reports_table(db_session)

    def test_skips_predecessors_outside_window(self, db_session: Session):
        from app.graph_extension import failed_after_edges

        _ensure_incident_reports_table(db_session)
        try:
            preceder = _make_skill_with_tags(db_session, "preceder", ["x"])
            victim = _make_skill_with_tags(db_session, "victim", ["y"])
            db_session.commit()

            t0 = datetime.now(timezone.utc)
            # 10 minutes before — beyond the 5-minute window
            db_session.add(InstallEvent(
                id=uuid4(), skill_id=preceder.id, skill_slug=preceder.slug,
                created_at=t0 - timedelta(minutes=10),
            ))
            db_session.commit()
            _insert_incident(db_session, "victim", "X", t0)

            edges = failed_after_edges(db_session, "victim")
            assert edges == []
        finally:
            _drop_incident_reports_table(db_session)

    def test_min_weight_filter(self, db_session: Session):
        from app.graph_extension import failed_after_edges

        _ensure_incident_reports_table(db_session)
        try:
            preceder = _make_skill_with_tags(db_session, "preceder", ["x"])
            _make_skill_with_tags(db_session, "victim", ["y"])
            db_session.commit()

            t0 = datetime.now(timezone.utc)
            db_session.add(InstallEvent(
                id=uuid4(), skill_id=preceder.id, skill_slug=preceder.slug,
                created_at=t0 - timedelta(minutes=2),
            ))
            db_session.commit()
            # 1 incident → weight 1.0; min_weight 0.99 should keep it
            _insert_incident(db_session, "victim", "X", t0)

            assert failed_after_edges(db_session, "victim", min_weight=0.99)
            # min_weight 1.5 above max possible — empty
            assert failed_after_edges(db_session, "victim", min_weight=1.5) == []
        finally:
            _drop_incident_reports_table(db_session)


# ── 2. arch_compatible_with derivation ──────────────────────────────────

class TestArchCompatible:
    def test_returns_empty_when_column_missing(self, db_session: Session):
        from app.graph_extension import arch_compatible_edges

        _make_skill_with_tags(db_session, "alpha", ["x"])
        _make_skill_with_tags(db_session, "beta", ["x"])
        db_session.commit()

        # The model doesn't have host_fingerprint yet (A.9 hasn't shipped).
        edges = arch_compatible_edges(db_session, "alpha")
        assert edges == []

    def test_derives_when_column_present(self, db_session: Session):
        """Simulate post-A.9 state by adding the column at runtime."""
        from app.graph_extension import arch_compatible_edges

        # SQLite supports ADD COLUMN. We do this on the session's own
        # connection so the rest of the test sees it.
        db_session.execute(text(
            "ALTER TABLE install_events ADD COLUMN host_fingerprint VARCHAR(64)"
        ))
        db_session.flush()

        alpha = _make_skill_with_tags(db_session, "alpha", ["x"])
        beta = _make_skill_with_tags(db_session, "beta", ["x"])
        gamma = _make_skill_with_tags(db_session, "gamma", ["x"])
        db_session.flush()

        # alpha + beta share fingerprint "host-A"; gamma is on "host-Z".
        # Use the ORM so SQLAlchemy's UUID type adapter handles the
        # dialect-specific serialisation (hex vs dashed) consistently with
        # the FK target rows.
        from app.models import InstallEvent as _IE
        for slug, skill_obj, fp in [
            ("alpha", alpha, "host-A"),
            ("alpha", alpha, "host-B"),
            ("beta", beta, "host-A"),
            ("gamma", gamma, "host-Z"),
        ]:
            ie = _IE(
                id=uuid4(),
                skill_id=skill_obj.id,
                skill_slug=slug,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(ie)
            db_session.flush()
            # Direct UPDATE to set the test-only column without round-tripping
            # via the ORM (the model doesn't declare host_fingerprint yet).
            db_session.execute(
                text(
                    "UPDATE install_events SET host_fingerprint=:fp WHERE id=:id"
                ),
                {"fp": fp, "id": ie.id.hex},
            )
        db_session.flush()

        edges = arch_compatible_edges(db_session, "alpha")
        slugs = {e["skill_slug"] for e in edges}
        assert "beta" in slugs
        assert "gamma" not in slugs  # disjoint fingerprints

        # Strip the test-only column so other tests aren't affected.
        # SQLite >= 3.35 supports DROP COLUMN; older versions just leak the
        # column for the rest of the session, which is harmless.
        try:
            db_session.execute(text("ALTER TABLE install_events DROP COLUMN host_fingerprint"))
            db_session.flush()
        except Exception:
            pass


# ── 3. replaced_by — manual + auto-detection ────────────────────────────

class TestReplacedBy:
    def test_manual_replacement_surfaces_at_weight_one(self, db_session: Session):
        from app.graph_extension import replaced_by_edges

        old = _make_skill_with_tags(db_session, "old-skill", ["x"])
        new = _make_skill_with_tags(db_session, "new-skill", ["x"])
        db_session.add(SkillReplacement(
            id=uuid4(), source_id=old.id, target_id=new.id,
            reason="superseded", created_by="master",
        ))
        db_session.commit()

        edges = replaced_by_edges(db_session, "old-skill")
        assert len(edges) == 1
        assert edges[0]["skill_slug"] == "new-skill"
        assert edges[0]["weight"] == 1.0
        assert edges[0]["edge_type"] == "replaced_by"

    def test_pending_candidate_surfaces_with_capped_weight(self, db_session: Session):
        from app.graph_extension import replaced_by_edges

        old = _make_skill_with_tags(db_session, "flaky", ["x"])
        new = _make_skill_with_tags(db_session, "stable", ["x"])
        db_session.add(ReplacementCandidate(
            id=uuid4(), source_id=old.id, target_id=new.id,
            evidence_json={
                "incident_count": 12,
                "incident_share": 0.7,
                "co_invoke_weight": 0.5,
            },
            status="pending",
        ))
        db_session.commit()

        edges = replaced_by_edges(db_session, "flaky")
        assert len(edges) == 1
        assert edges[0]["skill_slug"] == "stable"
        # avg(0.7, 0.5) = 0.6, well under the 0.9 cap
        assert edges[0]["weight"] == pytest.approx(0.6)

    def test_rejected_candidate_is_hidden(self, db_session: Session):
        from app.graph_extension import replaced_by_edges

        old = _make_skill_with_tags(db_session, "flaky", ["x"])
        new = _make_skill_with_tags(db_session, "stable", ["x"])
        db_session.add(ReplacementCandidate(
            id=uuid4(), source_id=old.id, target_id=new.id,
            evidence_json={"incident_share": 0.9, "co_invoke_weight": 0.9},
            status="rejected",
        ))
        db_session.commit()

        assert replaced_by_edges(db_session, "flaky") == []

    def test_sweep_proposes_candidates_when_evidence_meets_threshold(
        self, db_session: Session
    ):
        """The cron sweep should flag a high-incident skill with a viable
        co-installed alternative that itself has fewer incidents."""
        from app.graph_extension import sweep_replacement_candidates

        _ensure_incident_reports_table(db_session)
        try:
            flaky = _make_skill_with_tags(db_session, "flaky", ["x"])
            stable = _make_skill_with_tags(db_session, "stable", ["x"])
            db_session.commit()

            # 60% of incidents are 'flaky', co-install signal is strong
            t = datetime.now(timezone.utc) - timedelta(days=1)
            for _ in range(6):
                _insert_incident(db_session, "flaky", "Boom", t)
            for _ in range(4):
                _insert_incident(db_session, "stable", "Boom", t)

            db_session.add(SkillDerivedEdge(
                id=uuid4(),
                source_slug="flaky", target_slug="stable",
                weight=0.5,
                signals={"jaccard": 0.0, "category": 0.0, "coinstall": 0.6},
            ))
            db_session.commit()

            inserted = sweep_replacement_candidates(db_session)
            db_session.commit()
            assert inserted == 1
            cands = db_session.query(ReplacementCandidate).all()
            assert len(cands) == 1
            assert cands[0].source_id == flaky.id
            assert cands[0].target_id == stable.id
            assert cands[0].status == "pending"
        finally:
            _drop_incident_reports_table(db_session)

    def test_sweep_skips_when_co_invoke_weight_below_threshold(self, db_session: Session):
        from app.graph_extension import sweep_replacement_candidates

        _ensure_incident_reports_table(db_session)
        try:
            flaky = _make_skill_with_tags(db_session, "flaky", ["x"])
            stable = _make_skill_with_tags(db_session, "stable", ["x"])
            db_session.commit()

            t = datetime.now(timezone.utc) - timedelta(days=1)
            for _ in range(8):
                _insert_incident(db_session, "flaky", "Boom", t)
            for _ in range(2):
                _insert_incident(db_session, "stable", "Boom", t)

            # Co-invoke too weak (0.1 < CO_INVOKED_MIN=0.3)
            db_session.add(SkillDerivedEdge(
                id=uuid4(),
                source_slug="flaky", target_slug="stable",
                weight=0.2, signals={"coinstall": 0.1},
            ))
            db_session.commit()

            assert sweep_replacement_candidates(db_session) == 0
            assert db_session.query(ReplacementCandidate).count() == 0
        finally:
            _drop_incident_reports_table(db_session)

    def test_sweep_is_idempotent(self, db_session: Session):
        from app.graph_extension import sweep_replacement_candidates

        _ensure_incident_reports_table(db_session)
        try:
            flaky = _make_skill_with_tags(db_session, "flaky", ["x"])
            stable = _make_skill_with_tags(db_session, "stable", ["x"])
            db_session.commit()

            t = datetime.now(timezone.utc) - timedelta(days=1)
            for _ in range(7):
                _insert_incident(db_session, "flaky", "Boom", t)
            for _ in range(3):
                _insert_incident(db_session, "stable", "Boom", t)
            db_session.add(SkillDerivedEdge(
                id=uuid4(),
                source_slug="flaky", target_slug="stable",
                weight=0.6, signals={"coinstall": 0.5},
            ))
            db_session.commit()

            n1 = sweep_replacement_candidates(db_session)
            db_session.commit()
            n2 = sweep_replacement_candidates(db_session)
            db_session.commit()

            assert n1 == 1
            assert n2 == 0  # second run sees existing candidate, no-ops
            assert db_session.query(ReplacementCandidate).count() == 1
        finally:
            _drop_incident_reports_table(db_session)


# ── 4. /api/graph/related endpoint ──────────────────────────────────────

class TestEndpoint:
    def test_endpoint_validates_edge_type(
        self, graph_app: TestClient, db_session: Session
    ):
        _make_skill_with_tags(db_session, "alpha", ["x"])
        db_session.commit()
        r = graph_app.get("/api/graph/related?skill=alpha&edge=garbage")
        assert r.status_code == 422
        assert "unknown edge type" in r.json()["detail"]

    def test_endpoint_404_on_unknown_skill(self, graph_app: TestClient):
        r = graph_app.get("/api/graph/related?skill=nope&edge=related_skills")
        assert r.status_code == 404

    def test_endpoint_returns_declared_related(
        self, graph_app: TestClient, db_session: Session
    ):
        _make_skill_with_tags(db_session, "alpha", ["x"], related_skills=["beta"])
        _make_skill_with_tags(db_session, "beta", ["x"])
        db_session.commit()

        r = graph_app.get("/api/graph/related?skill=alpha&edge=related_skills")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["skill_slug"] == "beta"
        assert body[0]["edge_type"] == "related_skills"
        assert body[0]["weight"] == 1.0

    def test_endpoint_returns_tag_overlap_from_existing_g16_table(
        self, graph_app: TestClient, db_session: Session
    ):
        _make_skill_with_tags(db_session, "alpha", ["x"])
        _make_skill_with_tags(db_session, "beta", ["x"])
        db_session.add(SkillDerivedEdge(
            id=uuid4(),
            source_slug="alpha", target_slug="beta",
            weight=0.6,
            signals={"jaccard": 0.5, "category": 1.0, "coinstall": 0.0},
        ))
        db_session.commit()

        r = graph_app.get("/api/graph/related?skill=alpha&edge=tag_overlap")
        assert r.status_code == 200
        body = r.json()
        assert any(e["skill_slug"] == "beta" and e["weight"] == 0.5 for e in body)

    def test_endpoint_min_weight_filter(
        self, graph_app: TestClient, db_session: Session
    ):
        _make_skill_with_tags(db_session, "alpha", ["x"])
        _make_skill_with_tags(db_session, "beta", ["x"])
        _make_skill_with_tags(db_session, "gamma", ["x"])
        db_session.add(SkillDerivedEdge(
            id=uuid4(),
            source_slug="alpha", target_slug="beta",
            weight=0.8, signals={"jaccard": 0.8},
        ))
        db_session.add(SkillDerivedEdge(
            id=uuid4(),
            source_slug="alpha", target_slug="gamma",
            weight=0.2, signals={"jaccard": 0.2},
        ))
        db_session.commit()

        r = graph_app.get(
            "/api/graph/related?skill=alpha&edge=tag_overlap&min_weight=0.5"
        )
        body = r.json()
        slugs = {e["skill_slug"] for e in body}
        assert "beta" in slugs
        assert "gamma" not in slugs

    def test_endpoint_replaced_by_combines_manual_and_candidate(
        self, graph_app: TestClient, db_session: Session
    ):
        flaky = _make_skill_with_tags(db_session, "flaky", ["x"])
        stable = _make_skill_with_tags(db_session, "stable", ["x"])
        better = _make_skill_with_tags(db_session, "better", ["x"])
        db_session.add(SkillReplacement(
            id=uuid4(), source_id=flaky.id, target_id=stable.id,
            created_by="master",
        ))
        db_session.add(ReplacementCandidate(
            id=uuid4(), source_id=flaky.id, target_id=better.id,
            evidence_json={"incident_share": 0.6, "co_invoke_weight": 0.5},
            status="pending",
        ))
        db_session.commit()

        r = graph_app.get("/api/graph/related?skill=flaky&edge=replaced_by")
        body = r.json()
        # Manual (weight 1.0) sorts above candidate (weight 0.55)
        assert body[0]["skill_slug"] == "stable"
        assert body[0]["weight"] == 1.0
        assert any(e["skill_slug"] == "better" and e["weight"] < 1.0 for e in body)

    def test_endpoint_failed_after_empty_when_table_missing(
        self, graph_app: TestClient, db_session: Session
    ):
        # Belt-and-braces: if B.1 hasn't shipped, endpoint must be 200 [].
        _drop_incident_reports_table(db_session)
        _make_skill_with_tags(db_session, "victim", ["x"])
        db_session.commit()

        r = graph_app.get("/api/graph/related?skill=victim&edge=failed_after")
        assert r.status_code == 200
        assert r.json() == []

    def test_endpoint_arch_compatible_empty_when_column_missing(
        self, graph_app: TestClient, db_session: Session
    ):
        _make_skill_with_tags(db_session, "alpha", ["x"])
        db_session.commit()

        r = graph_app.get("/api/graph/related?skill=alpha&edge=arch_compatible_with")
        assert r.status_code == 200
        assert r.json() == []

    def test_endpoint_public_no_api_key_required(
        self, graph_app: TestClient, db_session: Session
    ):
        """The /api/graph prefix is in PUBLIC_PREFIXES, so no auth header
        is required. The TestClient here doesn't even mount the auth
        middleware, but the production wiring also exempts the prefix —
        verify the route handler itself doesn't reject."""
        _make_skill_with_tags(db_session, "alpha", ["x"])
        db_session.commit()
        r = graph_app.get("/api/graph/related?skill=alpha&edge=related_skills")
        assert r.status_code == 200

    def test_public_prefix_registered_in_middleware(self):
        from app.middleware import APIKeyMiddleware
        assert any(
            p == "/api/graph" or p.startswith("/api/graph")
            for p in APIKeyMiddleware.PUBLIC_PREFIXES
        ), f"/api/graph must be in PUBLIC_PREFIXES, got {APIKeyMiddleware.PUBLIC_PREFIXES}"

    def test_category_sibling_falls_back_to_db_when_no_signal_row(
        self, graph_app: TestClient, db_session: Session
    ):
        """category_sibling has a Cognee fallback path. With no derived
        edges and no Cognee installed, the DB fallback returns same-category
        public siblings."""
        _make_skill_with_tags(db_session, "alpha", ["x"], category="ops")
        _make_skill_with_tags(db_session, "beta", ["y"], category="ops")
        _make_skill_with_tags(db_session, "gamma", ["z"], category="content")
        db_session.commit()

        r = graph_app.get("/api/graph/related?skill=alpha&edge=category_sibling")
        body = r.json()
        slugs = {e["skill_slug"] for e in body}
        assert "beta" in slugs
        assert "gamma" not in slugs


# ── 5. POST /api/graph/replacements (master-only) ───────────────────────

class TestReplacementsWriteEndpoint:
    def test_post_requires_master_key(
        self, graph_app: TestClient, db_session: Session
    ):
        a = _make_skill_with_tags(db_session, "a", ["x"])
        b = _make_skill_with_tags(db_session, "b", ["x"])
        db_session.commit()
        r = graph_app.post(
            "/api/graph/replacements",
            json={"source_slug": "a", "target_slug": "b"},
        )
        assert r.status_code == 401

    def test_post_with_master_key_inserts(
        self, graph_app: TestClient, db_session: Session
    ):
        _make_skill_with_tags(db_session, "a", ["x"])
        _make_skill_with_tags(db_session, "b", ["x"])
        db_session.commit()
        r = graph_app.post(
            "/api/graph/replacements",
            json={"source_slug": "a", "target_slug": "b", "reason": "test"},
            headers={"x-api-key": settings.API_KEY},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["source_slug"] == "a"
        assert body["target_slug"] == "b"
        assert body["reason"] == "test"

    def test_post_rejects_self_replacement(
        self, graph_app: TestClient, db_session: Session
    ):
        _make_skill_with_tags(db_session, "a", ["x"])
        db_session.commit()
        r = graph_app.post(
            "/api/graph/replacements",
            json={"source_slug": "a", "target_slug": "a"},
            headers={"x-api-key": settings.API_KEY},
        )
        assert r.status_code == 422

    def test_get_lists_replacements_publicly(
        self, graph_app: TestClient, db_session: Session
    ):
        a = _make_skill_with_tags(db_session, "a", ["x"])
        b = _make_skill_with_tags(db_session, "b", ["x"])
        db_session.add(SkillReplacement(
            id=uuid4(), source_id=a.id, target_id=b.id, created_by="master",
        ))
        db_session.commit()
        r = graph_app.get("/api/graph/replacements")
        assert r.status_code == 200
        body = r.json()
        assert any(
            row["source_slug"] == "a" and row["target_slug"] == "b" for row in body
        )
