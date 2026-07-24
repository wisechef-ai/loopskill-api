"""B.7 — Canary state machine + rollback decision tests.

Covers:
  - STATIC/PROPERTY/SHADOW gate transitions (pass and fail)
  - rollback decision math (incident rate, sig rate, latency)
  - dwell-time enforcement at each canary tier
  - terminal states are sticky
  - /api/stats/patches endpoint shape
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.canary import (
    DWELL,
    Engine,
    NEXT_STAGE,
    Stage,
    StageMetrics,
    router as canary_router,
    should_rollback,
)
from app.database import get_db
from app.models import PatchCandidate, Skill


# ── Fakes ──────────────────────────────────────────────────────────────

class FakeMetrics:
    def __init__(self, m: StageMetrics):
        self.m = m

    def metrics_for(self, candidate_id, stage):
        return self.m


class FakeStatic:
    def __init__(self, has=True, passes=True):
        self.has, self.passes = has, passes

    def has_regression_test(self, _):
        return self.has

    def regression_test_passes(self, _):
        return self.passes


class FakeProperty:
    def __init__(self, ok=True):
        self.ok = ok

    def invariants_pass(self, _):
        return self.ok


class FakeShadow:
    def __init__(self, ok=True):
        self.ok = ok

    def shadow_diffs_clean(self, _):
        return self.ok


def _good_metrics() -> StageMetrics:
    return StageMetrics(
        incident_rate=1.0, baseline_rate=1.0,
        new_signatures=1, baseline_new_sig_rate=2.0,
        p95_latency_ms=100, baseline_p95_ms=100,
        sustained_hours=0,
    )


def _engine(metrics=None, static_=None, prop=None, shadow=None) -> Engine:
    return Engine(
        metrics=FakeMetrics(metrics or _good_metrics()),
        static_gate=static_ or FakeStatic(),
        property_gate=prop or FakeProperty(),
        shadow_gate=shadow or FakeShadow(),
    )


def _mk_candidate():
    return PatchCandidate(id=uuid4(), skill_id=uuid4(),
                          error_signature="x", status="pending")


# ── Rollback rules ─────────────────────────────────────────────────────

def test_rollback_when_incident_rate_high_for_4h():
    m = _good_metrics()
    m.incident_rate = 1.6
    m.sustained_hours = 4
    fired, reason = should_rollback(m)
    assert fired
    assert "incident rate" in reason


def test_no_rollback_when_high_rate_brief():
    m = _good_metrics()
    m.incident_rate = 1.6
    m.sustained_hours = 2
    fired, _ = should_rollback(m)
    assert not fired


def test_rollback_when_new_signatures_burst():
    m = _good_metrics()
    m.new_signatures = 10
    m.baseline_new_sig_rate = 2.0
    fired, reason = should_rollback(m)
    assert fired
    assert "new signatures" in reason


def test_rollback_when_latency_degrades_50pct():
    m = _good_metrics()
    m.p95_latency_ms = 200
    m.baseline_p95_ms = 100
    fired, reason = should_rollback(m)
    assert fired
    assert "latency" in reason


def test_no_rollback_within_thresholds():
    m = _good_metrics()
    m.incident_rate = 1.4
    m.sustained_hours = 12
    m.new_signatures = 2
    m.baseline_new_sig_rate = 2.0
    m.p95_latency_ms = 140
    m.baseline_p95_ms = 100
    fired, _ = should_rollback(m)
    assert not fired


# ── Static / property / shadow transitions ─────────────────────────────

def test_static_pass_advances_to_property():
    e = _engine(static_=FakeStatic(has=True, passes=True))
    next_stage, _ = e.step(_mk_candidate(), Stage.STATIC)
    assert next_stage == Stage.PROPERTY


def test_static_no_test_rejects():
    e = _engine(static_=FakeStatic(has=False, passes=False))
    next_stage, reason = e.step(_mk_candidate(), Stage.STATIC)
    assert next_stage == Stage.REJECTED
    assert "no regression test" in reason


def test_static_failing_test_rejects():
    e = _engine(static_=FakeStatic(has=True, passes=False))
    next_stage, _ = e.step(_mk_candidate(), Stage.STATIC)
    assert next_stage == Stage.REJECTED


def test_property_failure_rejects():
    e = _engine(prop=FakeProperty(ok=False))
    next_stage, _ = e.step(_mk_candidate(), Stage.PROPERTY)
    assert next_stage == Stage.REJECTED


def test_shadow_failure_rejects():
    e = _engine(shadow=FakeShadow(ok=False))
    next_stage, _ = e.step(_mk_candidate(), Stage.SHADOW)
    assert next_stage == Stage.REJECTED


def test_shadow_clean_advances_to_canary_1():
    e = _engine()
    next_stage, _ = e.step(_mk_candidate(), Stage.SHADOW)
    assert next_stage == Stage.CANARY_1


# ── Canary dwell + rollback ────────────────────────────────────────────

def test_canary_holds_during_dwell():
    e = _engine()
    now = datetime.now(timezone.utc)
    just_entered = now - timedelta(hours=1)
    next_stage, _ = e.step(_mk_candidate(), Stage.CANARY_1,
                            now=now, entered_at=just_entered)
    assert next_stage == Stage.CANARY_1


def test_canary_advances_after_dwell():
    e = _engine()
    now = datetime.now(timezone.utc)
    entered = now - DWELL[Stage.CANARY_1] - timedelta(seconds=1)
    next_stage, _ = e.step(_mk_candidate(), Stage.CANARY_1,
                            now=now, entered_at=entered)
    assert next_stage == Stage.CANARY_10


def test_canary_advances_through_all_tiers():
    e = _engine()
    chain = [Stage.CANARY_1, Stage.CANARY_10, Stage.CANARY_50]
    for stage in chain:
        now = datetime.now(timezone.utc)
        entered = now - DWELL[stage] - timedelta(seconds=1)
        next_stage, _ = e.step(_mk_candidate(), stage, now=now,
                                entered_at=entered)
        assert next_stage == NEXT_STAGE[stage]


def test_canary_rolls_back_when_metrics_bad():
    bad = _good_metrics()
    bad.incident_rate = 2.0
    bad.sustained_hours = 5
    e = _engine(metrics=bad)
    now = datetime.now(timezone.utc)
    next_stage, reason = e.step(_mk_candidate(), Stage.CANARY_10,
                                  now=now, entered_at=now)
    assert next_stage == Stage.ROLLED_BACK
    assert reason


def test_terminal_states_sticky():
    e = _engine()
    for terminal in [Stage.ROLLED_OUT, Stage.ROLLED_BACK, Stage.REJECTED]:
        next_stage, _ = e.step(_mk_candidate(), terminal)
        assert next_stage == terminal


# ── /api/stats/patches endpoint ────────────────────────────────────────

@pytest.fixture
def stats_client(db_session):
    app = FastAPI()
    app.include_router(canary_router)

    def override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _mk_patch(db, status):
    skill = Skill(id=uuid4(), slug=f"s-{status}-{uuid4().hex[:6]}",
                  title="x", is_public=True)
    db.add(skill)
    db.flush()
    p = PatchCandidate(
        id=uuid4(), skill_id=skill.id,
        error_signature=f"sig-{status}-{uuid4().hex[:6]}",
        cluster_count=3, distinct_agents=3, status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(p)
    db.flush()
    return p


def test_patch_stats_returns_status_breakdown(stats_client, db_session):
    for status in ["pending", "drafted", "canary", "rolled_out",
                    "rolled_back", "rejected"]:
        _mk_patch(db_session, status)
    db_session.commit()
    r = stats_client.get("/api/stats/patches?period=7d")
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "7d"
    assert body["pending"] == 1
    assert body["canary"] == 1
    assert body["rolled_out"] == 1
    assert body["rolled_back"] == 1
    assert body["rejected"] == 1
    assert body["by_status"]["drafted"] == 1


def test_patch_stats_invalid_period(stats_client):
    r = stats_client.get("/api/stats/patches?period=99x")
    assert r.status_code == 422


def test_patch_stats_default_is_7d(stats_client, db_session):
    _mk_patch(db_session, "drafted")
    db_session.commit()
    r = stats_client.get("/api/stats/patches")
    assert r.status_code == 200
    assert r.json()["period"] == "7d"
