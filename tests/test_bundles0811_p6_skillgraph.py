"""Tests for bundles0811 P6 — the skill graph stops being dark.

Covers:
  - the batch script actually calling build_edges/persist_edges (the thing
    that was missing in prod — 0 rows measured 2026-08-11)
  - app.graph_coverage honest per-edge-type coverage
  - GET /api/graph/coverage
  - GET /api/graph/neighborhood (lazy, paginated, advisory-only contract)
  - HARD CONSTRAINT: zero deep-link-classified federation_hub_skills bodies
    are ever fetched by any P6 code path
"""

from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from tests.conftest import make_skill
from tests.test_skill_derived_edges import make_skill_with_tags


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with just the graph router mounted (mirrors test_graph_extension.py's
    `graph_app` fixture) — the shared conftest `client` fixture doesn't mount
    `app.graph_routes`, so P6's new endpoints 404 against it."""
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


# ── 1. The builder actually runs end-to-end (script entry point) ──────────


class TestBuildSkillEdgesScript:
    def test_script_main_persists_rows(self, db_session: Session, monkeypatch):
        """scripts/build_skill_edges.py must call build_edges + persist_edges
        against a real session — the thing NOTHING in prod called before P6."""
        make_skill_with_tags(db_session, "a", ["docker", "ci"], category="devops")
        make_skill_with_tags(db_session, "b", ["docker", "ci"], category="devops")
        db_session.commit()

        import scripts.build_skill_edges as script

        monkeypatch.setattr(script, "SessionLocal", lambda: db_session)
        # Prevent the script's own commit/close from tearing down the
        # test's transactional session.
        monkeypatch.setattr(db_session, "commit", lambda: None)
        monkeypatch.setattr(db_session, "close", lambda: None)

        rc = script.main([])
        assert rc == 0

        from app.models import SkillDerivedEdge

        rows = db_session.query(SkillDerivedEdge).all()
        assert len(rows) >= 2, "builder ran but wrote nothing — still dark"


# ── 2. Honest per-edge-type coverage ───────────────────────────────────────


class TestGraphCoverage:
    def test_coverage_reports_all_five_edge_types(self, db_session: Session):
        from app.graph_coverage import compute_coverage

        make_skill_with_tags(db_session, "a", ["x", "y"], category="devops")
        db_session.commit()

        report = compute_coverage(db_session)
        assert set(report.keys()) == {
            "tag_overlap",
            "category_sibling",
            "co_install",
            "related_skills",
            "bundle_co_membership",
        }
        for edge_type, row in report.items():
            assert "eligible_nodes" in row, edge_type
            assert "covered_nodes" in row, edge_type
            assert "coverage_pct" in row, edge_type
            assert "last_built_at" in row, edge_type
            assert row["covered_nodes"] <= row["eligible_nodes"], edge_type

    def test_tag_overlap_coverage_reflects_real_build(self, db_session: Session):
        from app.edge_builder import build_edges, persist_edges
        from app.graph_coverage import compute_coverage

        make_skill_with_tags(db_session, "a", ["docker", "ci", "deploy"], category="devops")
        make_skill_with_tags(db_session, "b", ["docker", "ci", "deploy"], category="devops")
        make_skill_with_tags(db_session, "c", ["totally", "unrelated", "tags"], category="x")
        db_session.commit()
        persist_edges(db_session, build_edges(db_session))
        db_session.commit()

        report = compute_coverage(db_session)
        cov = report["tag_overlap"]
        assert cov["eligible_nodes"] == 3
        # a and b share full tag overlap -> covered; c has no match -> not covered
        assert cov["covered_nodes"] == 2
        assert cov["coverage_pct"] == pytest.approx(200 / 3, abs=0.5)
        assert cov["scope"] == "local-only"

    def test_co_install_coverage_is_local_only_and_honest(self, db_session: Session):
        """co_install coverage must never claim federated coverage — the
        InstallEvent.skill_id FK physically can't carry a federated identity."""
        from app.edge_builder import build_edges, persist_edges
        from app.graph_coverage import compute_coverage
        from app.models import APIKey, InstallEvent, User

        a = make_skill_with_tags(db_session, "a", ["x"], category="devops")
        b = make_skill_with_tags(db_session, "b", ["y"], category="devops")
        user = User(id=uuid4(), display_name="tester")
        db_session.add(user)
        db_session.flush()
        api_key = APIKey(id=uuid4(), user_id=user.id, key_prefix="rec_test", key_hash="hash")
        db_session.add(api_key)
        db_session.flush()
        db_session.add_all(
            [
                InstallEvent(id=uuid4(), skill_id=a.id, skill_slug="a", api_key_id=api_key.id),
                InstallEvent(id=uuid4(), skill_id=b.id, skill_slug="b", api_key_id=api_key.id),
            ]
        )
        db_session.commit()
        persist_edges(db_session, build_edges(db_session))
        db_session.commit()

        report = compute_coverage(db_session)
        cov = report["co_install"]
        assert cov["scope"] == "local-only"
        assert "federated" in cov["note"].lower()
        # only a,b installed -> eligible=2
        assert cov["eligible_nodes"] == 2

    def test_bundle_co_membership_spans_federated_identity(self, db_session: Session):
        """The ONE edge type allowed to span federated identity — via
        BundleSkill.federated_source/federated_slug, never a body fetch."""
        from app.graph_coverage import compute_coverage
        from app.models import Bundle, BundleSkill

        local = make_skill(db_session, slug="local-a")
        bundle = Bundle(id=uuid4(), name="test-bundle")
        db_session.add(bundle)
        db_session.flush()
        db_session.add(BundleSkill(id=uuid4(), bundle_id=bundle.id, skill_id=local.id, source="custom-added"))
        db_session.add(
            BundleSkill(
                id=uuid4(),
                bundle_id=bundle.id,
                skill_id=None,
                federated_source="clawhub",
                federated_slug="some-external-skill",
                source="custom-added",
            )
        )
        db_session.commit()

        report = compute_coverage(db_session)
        cov = report["bundle_co_membership"]
        assert cov["eligible_nodes"] == 2
        assert cov["covered_nodes"] == 2  # both share the same bundle -> co-membership
        assert "federated" in cov["scope"]

    def test_related_skills_reports_deferred_federated_eligible_count(self, db_session: Session):
        """Federated cross-ref extraction is DEFERRED — the count of eligible
        (licence-fetchable) origins must be real and computed, not fabricated."""
        from app.graph_coverage import compute_coverage
        from app.models import FederationHubSkill

        db_session.add(
            FederationHubSkill(
                slug="ext-fetchable-1",
                title="Ext",
                upstream_source="official",
                install_path="fetch_origin",
            )
        )
        db_session.add(
            FederationHubSkill(
                slug="ext-deep-link-1",
                title="Ext2",
                upstream_source="clawhub",
                install_path="deep_link",
            )
        )
        db_session.commit()

        report = compute_coverage(db_session)
        cov = report["related_skills"]
        assert cov["deferred_federated_eligible_origins"] == 1  # only fetch_origin counted


# ── 3. GET /api/graph/coverage ──────────────────────────────────────────────


class TestCoverageEndpoint:
    def test_endpoint_returns_all_edge_types(self, client: TestClient, db_session: Session):
        make_skill_with_tags(db_session, "a", ["x"], category="devops")
        db_session.commit()
        r = client.get("/api/graph/coverage")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body["edge_types"].keys()) == {
            "tag_overlap",
            "category_sibling",
            "co_install",
            "related_skills",
            "bundle_co_membership",
        }


# ── 4. GET /api/graph/neighborhood — lazy, paginated, advisory-only ────────


class TestNeighborhoodEndpoint:
    def test_neighborhood_paginates(self, client: TestClient, db_session: Session):
        from app.edge_builder import build_edges, persist_edges

        make_skill_with_tags(db_session, "hub", ["x", "y", "z"], category="devops")
        for i in range(5):
            make_skill_with_tags(db_session, f"spoke{i:02d}", ["x", "y", "z"], category="devops")
        db_session.commit()
        persist_edges(db_session, build_edges(db_session))
        db_session.commit()

        r1 = client.get("/api/graph/neighborhood", params={"skill": "hub", "limit": 2})
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert len(body1["items"]) == 2
        assert body1["next_cursor"] is not None
        assert body1["advisory_only"] is True

        r2 = client.get(
            "/api/graph/neighborhood",
            params={"skill": "hub", "limit": 2, "cursor": body1["next_cursor"]},
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        # No overlap between pages
        page1_slugs = {i["skill_slug"] for i in body1["items"]}
        page2_slugs = {i["skill_slug"] for i in body2["items"]}
        assert not (page1_slugs & page2_slugs)

    def test_neighborhood_is_advisory_only_never_installs(self, client: TestClient, db_session: Session):
        """The control-plane lock: this endpoint is read-only by construction
        (GET, no mutation). Assert the response never carries an install/apply
        affordance."""
        make_skill_with_tags(db_session, "solo", ["x"], category="devops")
        db_session.commit()
        r = client.get("/api/graph/neighborhood", params={"skill": "solo"})
        assert r.status_code == 200
        body = r.json()
        assert body["advisory_only"] is True
        assert "install" not in body["note"].lower() or "never" in body["note"].lower()
        # No field in the response schema names an install/apply action.
        for item in body["items"]:
            assert set(item.keys()) == {"skill_slug", "edge_type", "weight", "evidence_count"}

    def test_neighborhood_404_on_unknown_slug(self, client: TestClient):
        r = client.get("/api/graph/neighborhood", params={"skill": "does-not-exist"})
        assert r.status_code == 404

    def test_neighborhood_422_on_unknown_edge_type(self, client: TestClient, db_session: Session):
        make_skill(db_session, slug="known")
        db_session.commit()
        r = client.get("/api/graph/neighborhood", params={"skill": "known", "edge": "not-a-real-type"})
        assert r.status_code == 422

    def test_neighborhood_filters_by_edge_type(self, client: TestClient, db_session: Session):
        make_skill(db_session, slug="a", related_skills=["b"])
        make_skill(db_session, slug="b")
        db_session.commit()
        r = client.get("/api/graph/neighborhood", params={"skill": "a", "edge": "related_skills"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert all(i["edge_type"] == "related_skills" for i in body["items"])


# ── 5. HARD CONSTRAINT: zero deep-link bodies ever fetched ─────────────────


class TestZeroDeepLinkBodiesFetched:
    """P6 must never pull a deep-link-classified federation_hub_skills row's
    SKILL.md to compute any edge. Assert it structurally: neither the edge
    builder nor the coverage module import/call any HTTP-fetch primitive."""

    def test_edge_builder_has_no_network_imports(self):
        import inspect

        import app.edge_builder as mod

        source = inspect.getsource(mod)
        for forbidden in ("httpx", "requests", "urllib.request", "guarded_get", "guarded_head"):
            assert (
                forbidden not in source
            ), f"app.edge_builder must never fetch network content (found {forbidden!r})"

    def test_graph_coverage_has_no_network_imports(self):
        import inspect

        import app.graph_coverage as mod

        source = inspect.getsource(mod)
        for forbidden in ("httpx", "requests", "urllib.request", "guarded_get", "guarded_head"):
            assert (
                forbidden not in source
            ), f"app.graph_coverage must never fetch network content (found {forbidden!r})"

    def test_graph_routes_neighborhood_has_no_network_imports(self):
        """The lazy neighborhood endpoint resolves entirely from
        skill_derived_edges / Skill.related_skills — no origin fetch."""
        import inspect

        import app.graph_routes as mod

        source = inspect.getsource(mod)
        for forbidden in ("httpx", "requests", "urllib.request", "guarded_get", "guarded_head"):
            assert (
                forbidden not in source
            ), f"app.graph_routes must never fetch network content (found {forbidden!r})"

    def test_coverage_computation_makes_zero_http_calls(self, db_session: Session, monkeypatch):
        """Behavioral proof, not just a static grep: patch httpx.get/head to
        raise, seed deep-link rows, and confirm compute_coverage never trips
        the trap."""
        import httpx

        from app.graph_coverage import compute_coverage
        from app.models import FederationHubSkill

        def _boom(*a, **k):
            raise AssertionError("HTTP fetch attempted during coverage computation")

        monkeypatch.setattr(httpx, "get", _boom)
        monkeypatch.setattr(httpx, "head", _boom)

        for i in range(5):
            db_session.add(
                FederationHubSkill(
                    slug=f"deep-{i}",
                    title="Deep Link Row",
                    upstream_source="clawhub",
                    install_path="deep_link",
                )
            )
        db_session.commit()

        # Must not raise.
        compute_coverage(db_session)
