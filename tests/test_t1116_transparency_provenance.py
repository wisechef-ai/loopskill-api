"""t_a0607d92 — skill_error_rate_7d counts only skill errors, never crash reports.

The public transparency scorecard published ``skill_error_rate_7d`` as
``count(incident_reports in 7d) / count(telemetry installs in 7d)``.
``incident_reports`` holds two populations with two different meanings, so the
numerator was the union of:

  * crash reports — ``POST /api/feedback/incident`` (the auto-improve tooling
    crashed; says nothing about any marketplace skill), and
  * agent skill errors — ``POST /api/v1/skill-error`` and MCP
    ``loopskill_report_skill_error`` (an agent says a skill it ran misbehaved).

No user ever saw the conflation, because the metric's own bug (it filtered on a
non-existent ``IncidentReport.created_at``; a blanket ``except Exception`` turned
the AttributeError into 0.0) pinned it at 0.0 for its entire existence. Repairing
that crash without this change would have shipped a mixed-population number to a
public, unauthenticated endpoint. This file pins the separation.

The metric name is deliberately UNCHANGED: it is a public contract on a public
scorecard, and renaming it the moment it starts working reads as moving the
goalposts. What changed is which rows may enter its numerator.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models import IncidentReport, TelemetryEvent
from app.transparency_routes import (
    _compute_crash_report_count_7d,
    _compute_skill_error_rate_7d,
    _incident_report_counts_7d,
    _installs_7d,
)
from tests.conftest import make_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
_SIG = "deadbeef12345678"


@pytest.fixture()
def transparency_client(db_session, monkeypatch):
    """Same hermeticity as tests/test_transparency.py — no live GitHub calls."""
    from tests._app_factory import build_test_app

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_DISPATCH_PAT", raising=False)
    # The module caches the payload for 60s; each test needs a fresh read.
    import app.transparency_routes as tr

    monkeypatch.setattr(tr, "_cache_payload", None)
    monkeypatch.setattr(tr, "_cache_ts", 0.0)

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _row(db, skill, *, report_type=None, command=None, exit_code=None, env=None, age_days=0):
    r = IncidentReport(
        skill_id=skill.id,
        error_signature=f"sig{uuid.uuid4().hex[:24]}",
        env_fingerprint=env if env is not None else {},
        agent_fp_anon="agent-fp-test",
        occurred_at=datetime.now(UTC) - timedelta(days=age_days),
        command=command,
        exit_code=exit_code,
        stack_trace_top="module.py:1:f | boom",
    )
    if report_type is not None:
        r.report_type = report_type
    db.add(r)
    db.commit()
    return r


def _installs(db, n, age_days=0):
    for _ in range(n):
        db.add(
            TelemetryEvent(
                id=uuid.uuid4(),
                event_type="install",
                created_at=datetime.now(UTC) - timedelta(days=age_days),
            )
        )
    db.commit()


# ── the split itself ─────────────────────────────────────────────────────────


def test_counts_helper_splits_the_two_populations(db_session):
    s = make_skill(db_session, slug="split-target")
    for _ in range(3):
        _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR)
    for _ in range(5):
        _row(
            db_session,
            s,
            report_type=IncidentReport.TYPE_INCIDENT,
            command="skill-run --foo",
            exit_code=1,
            env={"os": "linux"},
        )

    skill_errors, crashes = _incident_report_counts_7d(db_session)
    assert (skill_errors, crashes) == (3, 5)


def test_metric_ignores_crash_reports(db_session):
    """THE defect, stated as an assertion.

    Before: 2 skill errors + 8 crashes over 10 installs read as 1.0 (capped) —
            the crash reports alone were enough to saturate the public metric.
    Now: it reads 2/10; the crashes belong to crash_report_count_7d.
    """
    s = make_skill(db_session, slug="metric-target")
    _installs(db_session, 10)
    for _ in range(2):
        _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR)
    for _ in range(8):
        _row(
            db_session,
            s,
            report_type=IncidentReport.TYPE_INCIDENT,
            command="run",
            exit_code=2,
            env={"os": "darwin"},
        )

    assert _compute_skill_error_rate_7d(db_session) == pytest.approx(0.2)
    assert _compute_crash_report_count_7d(db_session) == 8


def test_metric_is_no_longer_saturated_by_crashes_alone(db_session):
    """The precise shape of the old bug: zero skill errors, plenty of crashes.

    Pre-fix this published 1.0 — "every install errored" — on a skill nobody
    reported a problem with.
    """
    s = make_skill(db_session, slug="crash-only")
    _installs(db_session, 4)
    for _ in range(6):
        _row(db_session, s, report_type=IncidentReport.TYPE_INCIDENT, command="run", exit_code=1)

    assert _compute_skill_error_rate_7d(db_session) == 0.0
    assert _compute_crash_report_count_7d(db_session) == 6


def test_old_rows_outside_the_window_do_not_count(db_session):
    s = make_skill(db_session, slug="stale-target")
    _installs(db_session, 4)
    _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR, age_days=30)
    _row(
        db_session,
        s,
        report_type=IncidentReport.TYPE_INCIDENT,
        age_days=30,
        command="x",
        exit_code=1,
    )

    assert _incident_report_counts_7d(db_session) == (0, 0)
    assert _compute_skill_error_rate_7d(db_session) == 0.0


def test_unclassified_rows_never_inflate_the_skill_error_metric(db_session):
    """A row whose provenance is unknown (a future rail that forgot to classify)
    must land on the crash side. Understating a skill-error rate is survivable;
    inflating it with unrelated crashes misleads whoever is judging a creator's
    skill on a public page.
    """
    s = make_skill(db_session, slug="legacy-target")
    _installs(db_session, 10)
    _row(db_session, s, report_type=None)  # server_default -> 'incident'

    skill_errors, crashes = _incident_report_counts_7d(db_session)
    assert skill_errors == 0
    assert crashes == 1
    assert _compute_skill_error_rate_7d(db_session) == 0.0


def test_denominator_is_untouched(db_session):
    """The denominator stays "TelemetryEvent install rows in 7d", unchanged.

    Pinned so a later edit cannot silently widen this PR's scope into the second
    public contract (the rate's base) that the card put out of scope.
    """
    make_skill(db_session, slug="denominator-target")
    _installs(db_session, 7)
    # A non-install event and an out-of-window install must not count.
    db_session.add(TelemetryEvent(id=uuid.uuid4(), event_type="page_view", created_at=datetime.now(UTC)))
    db_session.add(
        TelemetryEvent(
            id=uuid.uuid4(), event_type="install", created_at=datetime.now(UTC) - timedelta(days=30)
        )
    )
    db_session.commit()

    assert _installs_7d(db_session) == 7


# ── the public payload ───────────────────────────────────────────────────────


def test_public_payload_exposes_both_halves(transparency_client, db_session):
    s = make_skill(db_session, slug="payload-target")
    _installs(db_session, 5)
    _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR)
    _row(db_session, s, report_type=IncidentReport.TYPE_INCIDENT, command="x", exit_code=9)

    r = transparency_client.get("/api/health/transparency")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skill_error_rate_7d"] == pytest.approx(0.2)
    assert body["crash_report_count_7d"] == 1
    # The name is a public contract — a rename here is a breaking change, not a
    # cleanup. This assertion exists to make that deliberate.
    assert "skill_error_rate_7d" in body


def test_payload_reads_incident_reports_exactly_once(transparency_client, db_session, monkeypatch):
    """The snapshot-consistency claim in _build_payload, pinned.

    The docstring says the rate and the crash count can never come from different
    snapshots. That is only true because _build_payload does ONE grouped read and
    hands both halves down. If someone "cleans this up" back into two
    _compute_*() calls, a row landing between the two queries would publish a
    rate and a count that never both existed — and nothing else here would notice.
    """
    from app import transparency_routes as tr

    calls: list[tuple[int, int]] = []
    real = tr._incident_report_counts_7d

    def spy(db):
        result = real(db)
        calls.append(result)
        return result

    monkeypatch.setattr(tr, "_incident_report_counts_7d", spy)
    monkeypatch.setattr(tr, "_cache_payload", None)
    monkeypatch.setattr(tr, "_cache_ts", 0.0)

    s = make_skill(db_session, slug="single-read-target")
    _installs(db_session, 4)
    _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR)
    _row(db_session, s, report_type=IncidentReport.TYPE_INCIDENT, command="x", exit_code=3)

    r = transparency_client.get("/api/health/transparency")
    assert r.status_code == 200, r.text
    assert len(calls) == 1, f"_build_payload read incident_reports {len(calls)}x, expected 1"
    assert calls[0] == (1, 1)
    body = r.json()
    assert body["skill_error_rate_7d"] == pytest.approx(0.25)
    assert body["crash_report_count_7d"] == 1


# ── writers must classify, and clients must not be able to ───────────────────


def test_crash_route_stamps_incident_provenance(db_session, monkeypatch):
    """End to end through POST /api/feedback/incident."""
    from app.feedback_routes import _reset_rate_limits, router as feedback_router
    from app.database import get_db
    from fastapi import FastAPI

    _reset_rate_limits()
    app = FastAPI()
    app.include_router(feedback_router)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, raise_server_exceptions=True)
    s = make_skill(db_session, slug="crash-route")

    r = client.post(
        "/api/feedback/incident",
        json={
            "skill_id": str(s.id),
            "error_signature": _SIG,
            "env_fingerprint": {"os": "linux", "arch": "x86_64"},
            "agent_fp_anon": "agent-fp-crash-route",
            "occurred_at": datetime.now(UTC).isoformat(),
            "command": "skill-run --foo bar",
            "exit_code": 1,
            "stack_trace_top": "module.py:42:f | raise ValueError('x')",
        },
    )
    assert r.status_code == 201, r.text
    row = (
        db_session.query(IncidentReport)
        .filter(IncidentReport.skill_id == s.id)
        .order_by(IncidentReport.occurred_at.desc())
        .first()
    )
    assert row is not None
    assert row.report_type == IncidentReport.TYPE_INCIDENT


def test_client_cannot_relabel_a_crash_report_as_a_skill_error(db_session):
    """The security half of the card.

    A client that could set report_type could move a public metric: POST a crash
    report wearing 'skill_error' and it would inflate every creator's error rate.
    IncidentIn has no such field and the row is built from explicit kwargs, so the
    key must be dropped — asserted here against the live route, not from reading
    the model.
    """
    from app.database import get_db
    from app.feedback_routes import _reset_rate_limits, router as feedback_router
    from fastapi import FastAPI

    _reset_rate_limits()
    app = FastAPI()
    app.include_router(feedback_router)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, raise_server_exceptions=True)
    s = make_skill(db_session, slug="crash-spoof")

    r = client.post(
        "/api/feedback/incident",
        json={
            "skill_id": str(s.id),
            "error_signature": _SIG,
            "env_fingerprint": {"os": "linux"},
            "agent_fp_anon": "agent-fp-spoof",
            "occurred_at": datetime.now(UTC).isoformat(),
            "command": "skill-run --foo",
            "exit_code": 1,
            "stack_trace_top": "module.py:1:f | boom",
            # The attack: a client trying to buy itself a skill-error row.
            "report_type": "skill_error",
        },
    )
    assert r.status_code == 201, r.text
    row = (
        db_session.query(IncidentReport)
        .filter(IncidentReport.skill_id == s.id)
        .order_by(IncidentReport.occurred_at.desc())
        .first()
    )
    assert row.report_type == IncidentReport.TYPE_INCIDENT


def test_skill_error_route_stamps_skill_error_provenance(db_session, monkeypatch):
    """End to end through POST /api/v1/skill-error (writer 1)."""
    from tests._app_factory import build_test_app

    monkeypatch.delenv("RECIPES_REPORT_ERRORS", raising=False)
    from app import feedback_ratelimit, skill_error_routes

    class _AllowedRL:
        allowed = True
        deduped = False
        issue_url = None
        force_available = False

    monkeypatch.setattr(feedback_ratelimit, "check_and_record", lambda **kw: _AllowedRL())
    monkeypatch.setattr(feedback_ratelimit, "check_skill_error_backstop", lambda identity: True)
    monkeypatch.setattr(skill_error_routes, "_check_rate_limit", lambda fp: True)

    s = make_skill(db_session, slug="skill-error-route")
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    client = TestClient(app, raise_server_exceptions=True)

    from app.config import settings

    key = getattr(settings, "API_KEY", None) or "provenance-test-master-key"
    monkeypatch.setattr(settings, "API_KEY", key, raising=False)

    r = client.post(
        "/api/v1/skill-error",
        headers={"x-api-key": key},
        json={
            "skill_slug": "skill-error-route",
            "error_signature": _SIG,
            "agent_fp_anon": "agentfp-provenance-0001",
            "stack_trace_top": "boom",
            # The counter-spoof: try to demote a genuine skill error to a crash.
            "report_type": "incident",
        },
    )
    assert r.status_code == 201, r.text
    row = (
        db_session.query(IncidentReport)
        .filter(IncidentReport.skill_id == s.id)
        .order_by(IncidentReport.occurred_at.desc())
        .first()
    )
    assert row is not None
    assert row.report_type == IncidentReport.TYPE_SKILL_ERROR


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("app/feedback_routes.py", "IncidentReport.TYPE_INCIDENT"),
        ("app/skill_error_routes.py", "IncidentReport.TYPE_SKILL_ERROR"),
        ("app/mcp/tools/skill_error.py", "IncidentReport.TYPE_SKILL_ERROR"),
    ],
)
def test_every_writer_stamps_provenance(module: str, expected: str):
    """All three construction sites pass report_type explicitly.

    A writer that omits it silently inherits the 'incident' server_default and the
    skill-error metric quietly starts under-counting — the same class of bug as
    counting everything, just in the opposite direction, and just as invisible.
    """
    src = (REPO_ROOT / module).read_text()
    assert re.search(rf"report_type\s*=\s*{re.escape(expected)}\b", src), (
        f"{module} builds an IncidentReport without stamping {expected}"
    )


def test_no_writer_accepts_provenance_from_a_client():
    """Structural backstop to the two route-level spoof tests.

    If someone ever converts a write site to ``IncidentReport(**payload.model_dump())``
    (or adds report_type to a request model), provenance becomes client-controlled
    and this fails — even if the route-level tests above were forgotten.
    """
    for module in ("app/feedback_routes.py", "app/skill_error_routes.py"):
        src = (REPO_ROOT / module).read_text()
        assert "IncidentReport(**" not in src, f"{module} splats a client payload into IncidentReport"
        cls = "IncidentIn" if "feedback_routes" in module else "SkillErrorIn"
        body = src.split(f"class {cls}(BaseModel):", 1)[1].split("\nclass ", 1)[0]
        assert "report_type" not in body, f"{cls} exposes report_type to clients"


def test_mcp_tool_takes_no_provenance_argument():
    """The MCP surface publishes an inputSchema agents pin to; provenance must
    not become a tool argument (that would make the metric client-settable and
    would also be undeclared drift in the mcp_tools parity slice)."""
    from app.mcp.tools.skill_error import loopskill_report_skill_error
    import inspect

    params = inspect.signature(loopskill_report_skill_error).parameters
    assert "report_type" not in params


def test_model_declares_the_vocabulary():
    assert IncidentReport.TYPE_INCIDENT == "incident"
    assert IncidentReport.TYPE_SKILL_ERROR == "skill_error"
    col = IncidentReport.__table__.columns["report_type"]
    assert col.nullable is False, "an unclassifiable row re-opens the conflation"
    assert col.index is True, "the transparency query filters on this column"
    assert col.server_default is not None, "a writer that forgets must land on 'incident'"
    assert col.server_default.arg == IncidentReport.TYPE_INCIDENT
