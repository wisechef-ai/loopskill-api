"""B.4 — Incident clustering cron tests.

Covers:
  - threshold edge cases (3 distinct agents → cluster; <3 → no cluster)
  - 24h window cutoff
  - upsert idempotency
  - status preservation when candidate already advanced past 'pending'
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.crons.incident_clustering import (
    Cluster,
    find_clusters,
    run_once,
    upsert_candidate,
)
from app.models import IncidentReport, PatchCandidate, Skill


SIG_A = "a" * 64
SIG_B = "b" * 64


def _mk_skill(db, slug="cluster-target"):
    s = Skill(id=uuid4(), slug=slug, title="cluster target", is_public=True)
    db.add(s)
    db.flush()
    return s


def _mk_incident(db, skill, sig, agent, *, occurred_at=None, report_type=None):
    """Seed one incident_reports row for clustering tests.

    ``report_type`` defaults to ``skill_error`` and is stated rather than left
    to the server default. It used to be omitted entirely, so EVERY row in this
    file landed on the default ``incident`` — a crash report from the
    auto-improve tooling — and the whole suite proved "qualifying incidents in,
    a patch_candidate out" using rows that ``find_clusters`` now (correctly)
    refuses to cluster, because the drafter downstream opens PRs against
    marketplace SKILLS. That made the suite a bystander to the very conflation
    t_a0607d92 fixed in ``app/transparency_routes.py``.

    ``command`` / ``exit_code`` / ``stack_trace_top`` are crash-report fields:
    the real skill_error population has them NULL (measured on prod 2026-11-16:
    11 of 11 rows with command/exit_code NULL), so they are only populated for
    the crash population. Clustering never reads them.
    """
    report_type = report_type or IncidentReport.TYPE_SKILL_ERROR
    crash_shaped = report_type == IncidentReport.TYPE_INCIDENT
    r = IncidentReport(
        id=uuid4(),
        skill_id=skill.id,
        error_signature=sig,
        report_type=report_type,
        env_fingerprint={"os": "linux"},
        agent_fp_anon=agent,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        command="x" if crash_shaped else None,
        exit_code=1 if crash_shaped else None,
        stack_trace_top="module.py:1:fn" if crash_shaped else None,
    )
    db.add(r)
    db.flush()
    return r


def test_threshold_three_distinct_agents_clusters(db_session):
    skill = _mk_skill(db_session)
    for agent in ["a1", "a2", "a3"]:
        _mk_incident(db_session, skill, SIG_A, agent)
    clusters = find_clusters(db_session)
    assert len(clusters) == 1
    assert clusters[0].error_signature == SIG_A
    assert clusters[0].cluster_count == 3
    assert clusters[0].distinct_agents == 3


def test_threshold_below_three_does_not_cluster(db_session):
    skill = _mk_skill(db_session)
    for agent in ["a1", "a2"]:
        _mk_incident(db_session, skill, SIG_A, agent)
    assert find_clusters(db_session) == []


def test_threshold_three_reports_one_agent_does_not_cluster(db_session):
    """Three reports but all from one agent — single noisy machine, not a fleet pattern."""
    skill = _mk_skill(db_session)
    for _ in range(3):
        _mk_incident(db_session, skill, SIG_A, "lone-agent")
    assert find_clusters(db_session) == []


def test_window_excludes_old_reports(db_session):
    skill = _mk_skill(db_session)
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    for agent in ["a1", "a2", "a3"]:
        _mk_incident(db_session, skill, SIG_A, agent, occurred_at=old)
    assert find_clusters(db_session) == []


def test_distinct_signatures_clustered_separately(db_session):
    skill = _mk_skill(db_session)
    for agent in ["a1", "a2", "a3"]:
        _mk_incident(db_session, skill, SIG_A, agent)
    for agent in ["b1", "b2", "b3"]:
        _mk_incident(db_session, skill, SIG_B, agent)
    clusters = find_clusters(db_session)
    sigs = {c.error_signature for c in clusters}
    assert sigs == {SIG_A, SIG_B}


def test_run_once_creates_patch_candidate(db_session):
    skill = _mk_skill(db_session)
    for agent in ["a1", "a2", "a3"]:
        _mk_incident(db_session, skill, SIG_A, agent)
    db_session.flush()
    n = run_once(db=db_session)
    assert n == 1
    row = db_session.query(PatchCandidate).one()
    assert row.status == "pending"
    assert row.error_signature == SIG_A
    assert row.cluster_count == 3
    assert row.distinct_agents == 3


def test_run_once_idempotent_does_not_duplicate(db_session):
    skill = _mk_skill(db_session)
    for agent in ["a1", "a2", "a3"]:
        _mk_incident(db_session, skill, SIG_A, agent)
    db_session.flush()
    run_once(db=db_session)
    run_once(db=db_session)
    rows = db_session.query(PatchCandidate).all()
    assert len(rows) == 1


def test_upsert_preserves_advanced_status(db_session):
    """If a candidate has advanced to 'canary', re-clustering must not
    drag it back to 'pending'. Only metrics + last_clustered_at change."""
    skill = _mk_skill(db_session)
    cand = PatchCandidate(
        id=uuid4(),
        skill_id=skill.id,
        error_signature=SIG_A,
        cluster_count=3,
        distinct_agents=3,
        status="canary",
    )
    db_session.add(cand)
    db_session.flush()

    cluster = Cluster(skill_id=skill.id, error_signature=SIG_A, cluster_count=99, distinct_agents=12)
    upsert_candidate(db_session, cluster)
    db_session.flush()

    refreshed = db_session.query(PatchCandidate).filter_by(skill_id=skill.id, error_signature=SIG_A).one()
    assert refreshed.status == "canary"
    assert refreshed.cluster_count == 99
    assert refreshed.distinct_agents == 12


def test_crash_reports_never_cluster_into_skill_fixes(db_session):
    """incident_reports holds two populations (t_a0607d92). A crash report is
    the auto-improve TOOLING dying and says nothing about a marketplace skill,
    yet the drafter downstream writes PR-ready fixes against skill directories —
    so clustering must never mint a candidate from this population."""
    skill = _mk_skill(db_session, slug="crash-only")
    for agent in ["a1", "a2", "a3"]:
        _mk_incident(db_session, skill, SIG_A, agent, report_type=IncidentReport.TYPE_INCIDENT)
    assert find_clusters(db_session) == []
    assert run_once(db=db_session) == 0
    assert db_session.query(PatchCandidate).count() == 0


def test_mixed_populations_cannot_cross_the_distinct_agent_bar(db_session):
    """2 skill errors + 1 crash report = 3 distinct agents on one
    (skill, signature). The UNION clears the >= 3 bar while neither population
    supports the cluster on its own, so the bar must be per-population."""
    skill = _mk_skill(db_session, slug="mixed")
    _mk_incident(db_session, skill, SIG_A, "a1", report_type=IncidentReport.TYPE_SKILL_ERROR)
    _mk_incident(db_session, skill, SIG_A, "a2", report_type=IncidentReport.TYPE_SKILL_ERROR)
    _mk_incident(db_session, skill, SIG_A, "c1", report_type=IncidentReport.TYPE_INCIDENT)
    db_session.flush()
    assert find_clusters(db_session) == []
    assert run_once(db=db_session) == 0
    # the third agent being skill-shaped, however, is a real cluster
    _mk_incident(db_session, skill, SIG_A, "a3", report_type=IncidentReport.TYPE_SKILL_ERROR)
    assert len(find_clusters(db_session)) == 1
