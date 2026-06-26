"""Tests for demandbrief_3005 — GET /api/admin/demand-brief.

Covers:
  - build_demand_brief() pure function: scoring, ranking, graceful degradation
  - zero-result search themes (MissingSkillQuery → search_gap)
  - co-install cluster themes (SkillDerivedEdge → cookbook_bundle)
  - distribution-activation theme (installs exist, cookbooks/fleets empty)
  - master-key auth gate on the HTTP route (403 for non-master)
  - empty-DB safety (no 500, returns empty themes)
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.demand_routes import build_demand_brief, router as demand_router
from app.models import (
    Bundle,
    InstallEvent,
    MissingSkillQuery,
    SkillDerivedEdge,
)
from tests.conftest import make_skill


# ── pure-function tests (no HTTP, no auth) ───────────────────────────────────


def test_empty_db_returns_safe_brief(db_session):
    """No signals at all → valid brief, no themes, no crash."""
    brief = build_demand_brief(db_session)
    assert brief["top_theme"] is None
    assert brief["themes"] == []
    assert brief["window_days"] == 14
    assert "weights" in brief
    assert brief["catalog"]["public_skills"] == 0


def test_zero_result_searches_become_search_gap_themes(db_session):
    """MissingSkillQuery rows → ranked search_gap themes by hit count."""
    today = date.today()
    db_session.add_all(
        [
            MissingSkillQuery(id=uuid4(), query="invoice-reconciliation", day=today, count=12),
            MissingSkillQuery(id=uuid4(), query="tiktok-scheduler", day=today, count=3),
        ]
    )
    db_session.flush()

    brief = build_demand_brief(db_session)
    gaps = [t for t in brief["themes"] if t["kind"] == "search_gap"]
    assert len(gaps) == 2
    # Higher hit count ranks first within the search-gap block.
    assert gaps[0]["label"] == "invoice-reconciliation"
    assert "12 zero-result searches" in gaps[0]["evidence"]
    assert gaps[0]["build_first"] is True
    # search_demand normalized to top hit (12) → invoice=1.0, tiktok=3/12
    assert gaps[0]["score_components"]["search_demand"] == 1.0
    assert gaps[1]["score_components"]["search_demand"] == pytest.approx(0.25, abs=1e-3)


def test_coinstall_edges_become_cookbook_bundle_themes(db_session):
    """SkillDerivedEdge rows → cookbook_bundle themes, deduped to undirected."""
    db_session.add_all(
        [
            SkillDerivedEdge(
                id=uuid4(), source_slug="cold-outreach", target_slug="proposal-builder", weight=0.4
            ),
            # reverse direction of the same pair — must dedupe to one theme
            SkillDerivedEdge(
                id=uuid4(), source_slug="proposal-builder", target_slug="cold-outreach", weight=0.4
            ),
            SkillDerivedEdge(
                id=uuid4(), source_slug="seo-audit-engine", target_slug="whitelabel-dashboard", weight=0.2
            ),
        ]
    )
    db_session.flush()

    brief = build_demand_brief(db_session)
    bundles = [t for t in brief["themes"] if t["kind"] == "cookbook_bundle"]
    # 3 directed rows → 2 undirected pairs
    assert len(bundles) == 2
    labels = {b["label"] for b in bundles}
    assert "cold-outreach + proposal-builder" in labels
    # cookbook bundles carry the MRR-send content angle
    assert any("cookbook" in b["content_angle"].lower() for b in bundles)
    # high mrr_leverage by design
    assert bundles[0]["score_components"]["mrr_leverage"] == 0.9


def test_activation_theme_fires_when_installs_exist_but_no_cookbooks(db_session):
    """Real installs + zero adopted cookbooks/fleets → activation theme present."""
    skill = make_skill(db_session, slug="client-reporter")
    # 3 real installs (non-vanity) in the window
    for _ in range(3):
        db_session.add(
            InstallEvent(
                id=uuid4(),
                skill_id=skill.id,
                skill_slug="client-reporter",
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    db_session.flush()

    brief = build_demand_brief(db_session)
    activation = [t for t in brief["themes"] if t["kind"] == "distribution_activation"]
    assert len(activation) == 1
    assert activation[0]["score_components"]["mrr_leverage"] == 1.0
    assert "real skill installs" in activation[0]["evidence"]


def test_vanity_installs_excluded_from_activation_evidence(db_session):
    """super-memory installs are stripped from the 'real installs' count."""
    skill = make_skill(db_session, slug="super-memory")
    real = make_skill(db_session, slug="graphify")
    for _ in range(5):
        db_session.add(
            InstallEvent(
                id=uuid4(),
                skill_id=skill.id,
                skill_slug="super-memory",
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    db_session.add(
        InstallEvent(
            id=uuid4(),
            skill_id=real.id,
            skill_slug="graphify",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    db_session.flush()

    brief = build_demand_brief(db_session)
    activation = [t for t in brief["themes"] if t["kind"] == "distribution_activation"]
    assert len(activation) == 1
    # 6 raw installs, 5 vanity → 1 real
    assert "1 real skill installs" in activation[0]["evidence"]


def test_activation_theme_suppressed_when_cookbooks_and_fleets_active(db_session):
    """Distribution layer has traction → activation theme NOT surfaced."""
    skill = make_skill(db_session, slug="client-reporter")
    db_session.add(
        InstallEvent(
            id=uuid4(),
            skill_id=skill.id,
            skill_slug="client-reporter",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    # adopted (non-base) cookbook + a fleet → funnel is activated
    db_session.add(Bundle(id=uuid4(), name="agency-bundle", is_base=False))
    from app.models import Fleet

    db_session.add(Fleet(id=uuid4(), owner_user_id=uuid4(), name="my-fleet", fleet_api_key_hash="x" * 64))
    db_session.flush()

    brief = build_demand_brief(db_session)
    activation = [t for t in brief["themes"] if t["kind"] == "distribution_activation"]
    assert activation == []


def test_themes_ranked_by_score_descending(db_session):
    """top_theme is the highest-scoring theme across all blocks."""
    today = date.today()
    db_session.add(MissingSkillQuery(id=uuid4(), query="x-thing", day=today, count=5))
    db_session.add(SkillDerivedEdge(id=uuid4(), source_slug="a", target_slug="b", weight=0.5))
    db_session.flush()

    brief = build_demand_brief(db_session)
    scores = [t["score"] for t in brief["themes"]]
    assert scores == sorted(scores, reverse=True)
    assert brief["top_theme"]["score"] == scores[0]


# ── HTTP auth-gate tests ─────────────────────────────────────────────────────


def _make_app(db_session, *, master: bool) -> TestClient:
    """Local app mounting only the demand router, with a faked auth ctx."""
    app = FastAPI()

    def override_get_db():
        yield db_session

    @app.middleware("http")
    async def fake_auth(request, call_next):
        # master key → api_key_user_id is None; non-master → a uuid
        request.state.api_key_user_id = None if master else str(uuid4())
        return await call_next(request)

    app.include_router(demand_router)
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_route_allows_master_key(db_session):
    client = _make_app(db_session, master=True)
    resp = client.get("/api/admin/demand-brief")
    assert resp.status_code == 200
    body = resp.json()
    assert "themes" in body and "weights" in body


def test_route_forbids_non_master_key(db_session):
    client = _make_app(db_session, master=False)
    resp = client.get("/api/admin/demand-brief")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin only"


def test_route_respects_days_and_limit_params(db_session):
    today = date.today()
    old = today - timedelta(days=40)
    db_session.add_all(
        [
            MissingSkillQuery(id=uuid4(), query="recent", day=today, count=4),
            MissingSkillQuery(id=uuid4(), query="ancient", day=old, count=99),
        ]
    )
    db_session.flush()

    client = _make_app(db_session, master=True)
    # 7-day window excludes the 40-day-old query despite its high count
    resp = client.get("/api/admin/demand-brief?days=7&limit=5")
    assert resp.status_code == 200
    labels = {t["label"] for t in resp.json()["themes"]}
    assert "recent" in labels
    assert "ancient" not in labels


# ── CLI entrypoint (the P1 producer path) ────────────────────────────────────


def test_cli_emits_valid_json(db_session, monkeypatch, capsys):
    """`python -m app.demand_routes --json` prints a parseable brief.

    The CLI is the producer path: wisechef-hq runs it locally against its own
    DB. We patch SessionLocal to the test session and assert stdout is valid
    JSON with the expected top-level shape.
    """
    import app.demand_routes as dr

    today = date.today()
    db_session.add(MissingSkillQuery(id=uuid4(), query="cli-probe", day=today, count=7))
    db_session.flush()

    class _FakeSessionLocal:
        def __call__(self):
            return db_session

    # app.database.SessionLocal is imported inside _cli(); patch at source.
    import app.database

    monkeypatch.setattr(app.database, "SessionLocal", _FakeSessionLocal())
    # _cli closes the session; make close a no-op so the test fixture survives.
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr("sys.argv", ["demand_routes", "--json", "--days", "30"])

    rc = dr._cli()
    assert rc == 0

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "themes" in parsed
    assert parsed["window_days"] == 30
    assert any(t["label"] == "cli-probe" for t in parsed["themes"])


def test_cli_clamps_out_of_range_params(db_session, monkeypatch, capsys):
    """--days/--limit are clamped to valid ranges (defensive, no 422 in CLI)."""
    import app.database
    import app.demand_routes as dr

    monkeypatch.setattr(app.database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr("sys.argv", ["demand_routes", "--days", "999", "--limit", "0"])

    rc = dr._cli()
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["window_days"] == 90  # clamped from 999
