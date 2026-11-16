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

DENOMINATOR — updated deliberately by t_61421779 on 2026-11-16. t_a0607d92 shipped
with ``test_denominator_is_untouched`` asserting the base stayed
``TelemetryEvent(event_type='install')``, precisely so nobody could widen scope
into it by accident. That pin has now been MOVED ON PURPOSE, not removed: the base
is server-minted ``InstallEvent`` rows that are organic, non-probe and
``status='ok'``. The replacement test asserts BOTH directions — InstallEvent rows
count AND telemetry install rows no longer do — so reverting is as loud as
advancing was. See :func:`app.transparency_routes._installs_7d` for the measured
reasons.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models import APIKey, IncidentReport, InstallEvent, TelemetryEvent, User
from app.transparency_routes import (
    _compute_crash_report_count_7d,
    _compute_install_count_drift,
    _compute_skill_error_rate_7d,
    _incident_report_counts_7d,
    _installs_7d,
)
from tests.conftest import make_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
_SIG = "deadbeef12345678"
_EXTERNAL_IP = "54.83.240.58"


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


def _installs(db, n, age_days=0, *, skill=None, is_probe=False, status="ok", key=None, client_ip=None):
    """Seed ``n`` denominator rows — server-minted ``InstallEvent``s.

    These are the rows the rate is now computed against (t_61421779). Defaults are
    the shape of a real install a genuine user performed: organic (no key, no
    internal IP), not a probe, completed. Any row that should be EXCLUDED from the
    denominator is built by overriding one of those kwargs, so every filter in
    :func:`_installs_7d` has a test that had to name it explicitly.
    """
    sk = skill or make_skill(db, slug=f"base-{uuid.uuid4().hex[:12]}")
    for _ in range(n):
        db.add(
            InstallEvent(
                id=uuid.uuid4(),
                skill_id=sk.id,
                skill_slug=sk.slug,
                api_key_id=key.id if key is not None else None,
                version_semver="1.0.0",
                client_ip=client_ip,
                status=status,
                is_probe=is_probe,
                created_at=datetime.now(UTC) - timedelta(days=age_days),
            )
        )
    db.commit()
    return sk


def _telemetry_installs(db, n, age_days=0, *, skill_slug="telemetry-only"):
    """Seed client-self-reported telemetry installs — the OLD denominator.

    Kept so the file can still prove the negative: these must no longer move the
    metric. ``TelemetryEvent`` is written by ``POST /api/telemetry`` from a caller-
    supplied ``event_type``, which is the whole reason it lost the job.
    """
    for _ in range(n):
        db.add(
            TelemetryEvent(
                id=uuid.uuid4(),
                event_type="install",
                skill_slug=skill_slug,
                created_at=datetime.now(UTC) - timedelta(days=age_days),
            )
        )
    db.commit()


def _mk_agent_key(db, *, is_test=False, is_agent=False):
    """A key so the denominator can be tested against the shared organic predicate."""
    u = User(id=uuid.uuid4(), display_name=f"u-{uuid.uuid4().hex[:6]}", email=None, is_agent=is_agent)
    db.add(u)
    db.flush()
    raw = f"rec_live_{uuid.uuid4().hex}"
    k = APIKey(
        id=uuid.uuid4(),
        user_id=u.id,
        key_prefix=raw[:12],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        is_active=True,
        is_test=is_test,
        name="denominator-test-key",
    )
    db.add(k)
    db.flush()
    return k


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


# ── the denominator (t_61421779 — moved deliberately, see module docstring) ──


def test_denominator_is_server_minted_installs_not_telemetry(db_session):
    """The pin t_a0607d92 called ``test_denominator_is_untouched``, moved on purpose.

    It asserted the base was ``TelemetryEvent(event_type='install')`` and existed to
    make an accidental change to the rate's second public contract impossible. It is
    replaced — not deleted — by the same kind of guard pointed at the new base, and
    it tests BOTH directions so the swap cannot be quietly undone:

      * server-minted ``InstallEvent`` rows in the window DO count, and
      * client-self-reported ``TelemetryEvent`` install rows DO NOT, in either
        direction (a telemetry row neither raises nor lowers the base).

    Out-of-window installs and non-install event types stay out, as before.
    """
    make_skill(db_session, slug="denominator-target")
    _installs(db_session, 7)

    # The old base: 400 self-reported telemetry installs must not move the number.
    _telemetry_installs(db_session, 400)
    # A non-install event and an out-of-window install must not count either.
    db_session.add(TelemetryEvent(id=uuid.uuid4(), event_type="page_view", created_at=datetime.now(UTC)))
    db_session.add(
        InstallEvent(
            id=uuid.uuid4(),
            skill_id=make_skill(db_session, slug="stale-base").id,
            skill_slug="stale-base",
            status="ok",
            is_probe=False,
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
    )
    db_session.commit()

    assert _installs_7d(db_session) == 7


def test_denominator_excludes_probe_test_key_agent_key_and_unfinished(db_session, monkeypatch):
    """Each exclusion in :func:`_installs_7d` is named by its own row.

    Every one of these inflates the denominator, and an inflated denominator LOWERS
    a public quality metric — so each is a way to make the scorecard look better
    than the platform is. ``is_test`` / ``is_agent`` come from the ONE shared organic
    predicate rather than a local re-implementation, which is the point of reusing
    ``organic_install_predicate()``: if that definition ever widens, this metric
    widens with it instead of drifting.
    """
    monkeypatch.setattr("app.config.settings.SERVER_PUBLIC_IP", "77.42.92.141")
    monkeypatch.setattr("app.config.settings.KNOWN_INTERNAL_IPS", ["10.0.0.9"])
    sk = make_skill(db_session, slug="filtered-base")

    _installs(db_session, 3, skill=sk)  # the only three that should count
    _installs(db_session, 5, skill=sk, is_probe=True)  # our own fleet automation
    _installs(db_session, 5, skill=sk, status="rolled_back")  # never completed
    _installs(db_session, 5, skill=sk, status="partial")
    _installs(db_session, 5, skill=sk, key=_mk_agent_key(db_session, is_test=True))
    _installs(db_session, 5, skill=sk, key=_mk_agent_key(db_session, is_agent=True))
    _installs(db_session, 5, skill=sk, client_ip="77.42.92.141")  # on-host CI
    _installs(db_session, 5, skill=sk, client_ip="10.0.0.9")  # dogfood box
    _installs(db_session, 5, skill=sk, age_days=8)  # outside the window

    assert _installs_7d(db_session) == 3
    # And the same rows must not leak into the rate either.
    assert _compute_skill_error_rate_7d(db_session) == 0.0


def test_organic_external_keyed_install_still_counts(db_session, monkeypatch):
    """Counterweight to the filter test: a genuine keyed install is IN.

    Without this, the previous test would pass just as well against a denominator
    that counted nothing at all — an always-zero base is the exact defect on the
    card, so "0" is never an acceptable shape to prove a filter with.
    """
    monkeypatch.setattr("app.config.settings.SERVER_PUBLIC_IP", "77.42.92.141")
    sk = make_skill(db_session, slug="keyed-base")
    key = _mk_agent_key(db_session)  # ordinary human key: is_test=False, is_agent=False
    _installs(db_session, 4, skill=sk, key=key, client_ip=_EXTERNAL_IP)

    assert _installs_7d(db_session) == 4


def _installs_7d_ast():
    """The real ``_installs_7d`` code object as an AST, docstring excluded.

    These two guards inspect CODE, not prose — a substring check over the source
    would trip on the docstring, which has to name ``TelemetryEvent`` and the
    error-swallowing anti-pattern in order to explain why neither belongs there.
    """
    import ast

    src = (REPO_ROOT / "app" / "transparency_routes.py").read_text()
    fn = next(
        (n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_installs_7d"),
        None,
    )
    assert fn is not None, "_installs_7d is gone — the denominator lost its named helper"
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)  # the docstring is documentation, not behaviour
    return body


def test_installs_7d_never_laundering_db_error_into_a_zero():
    """Structural: the denominator must not be wrapped in an error-swallowing guard.

    The numerator helper catches ``SQLAlchemyError`` and returns 0 — defensible for
    a table that arrived late on self-hosted DBs. For the base it is the opposite:
    a DB error laundered into ``0`` installs publishes a rate of ``0.0``, i.e.
    "zero errors per install", on a public page. A broken scorecard has to be loud.
    """
    import ast

    body = _installs_7d_ast()
    handlers = [n for node in body for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]
    assert not handlers, f"_installs_7d swallows {len(handlers)} error(s) into a flattering 0 denominator"


def test_denominator_does_not_read_the_telemetry_table():
    """Structural backstop to the behavioural pin above.

    A route-level test can be forgotten during a refactor; this one cannot be talked
    into believing a telemetry-backed base was reintroduced. Checks resolved names,
    so a prose mention of ``TelemetryEvent`` in the docstring cannot false-fail it
    and an import renamed into the function cannot false-pass it.
    """
    import ast

    body = _installs_7d_ast()
    names = {n.id for node in body for n in ast.walk(node) if isinstance(n, ast.Name)}
    names |= {n.attr for node in body for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    assert "TelemetryEvent" not in names, "the denominator went back to the client-self-reported table"
    assert "InstallEvent" in names, "the denominator is not reading the server-minted table"


# ── the two metrics disagree about "an install" ON PURPOSE ───────────────────


def test_drift_metric_still_considers_telemetry_only_skills(db_session):
    """Pins the documented divergence between the two install populations.

    ``install_count_drift`` audits ``Skill.install_count``, which BOTH the
    InstallEvent writers and the telemetry writer bump — so it must keep seeing the
    telemetry rows, or telemetry-driven counter corruption becomes invisible. The
    rate deliberately stopped seeing them. Anyone tempted to "unify" the two helpers
    trips this test and has to argue with it first.
    """
    sk = make_skill(db_session, slug="telemetry-only-drift", install_count=0)
    _telemetry_installs(db_session, 6, skill_slug=str(sk.slug))
    _installs(db_session, 2, skill=sk)

    # Drift sees the larger of the two emitters: |0 - 6| = 6.
    assert _compute_install_count_drift(db_session) == 6
    # The rate sees only the server-minted 2.
    from app.transparency_routes import _installs_7d as _base

    assert _base(db_session) == 2


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


# ── the absolutes (t_61421779) ───────────────────────────────────────────────


def test_payload_publishes_the_absolutes_and_the_rate_is_their_quotient(transparency_client, db_session):
    """The rate alone was the transparency failure; the counts are the fix.

    ``skill_error_rate_7d`` keeps its name and value, but it is now published
    beside the two numbers it is made of, and must equal them exactly. A reader who
    cannot see the base cannot tell "0 errors in 0 installs" from "0 errors in
    4,000 installs" — which is the whole reason a denominator of 0 could sit on a
    public page reading 0.0 for five months without anyone noticing.
    """
    s = make_skill(db_session, slug="absolutes-target")
    _installs(db_session, 8)
    for _ in range(2):
        _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR)

    body = transparency_client.get("/api/health/transparency").json()
    assert body["skill_error_count_7d"] == 2
    assert body["installs_7d"] == 8
    assert body["skill_error_rate_7d"] == pytest.approx(0.25)
    # The quotient is derivable from the absolutes — never let them drift apart.
    assert body["skill_error_rate_7d"] == pytest.approx(
        round(min(1.0, body["skill_error_count_7d"] / body["installs_7d"]), 6)
    )


def test_zero_base_is_now_distinguishable_from_a_clean_sheet(transparency_client, db_session):
    """The 0.0 guard stays (public type contract) but stops being a lie by omission.

    With no installs at all the rate is still 0.0 — changing that would be a type
    change on a public endpoint — yet the payload now says so out loud via
    ``installs_7d: 0``. Before this card, ``0.0`` with no base was the ONLY shape
    the endpoint could ever produce.
    """
    body = transparency_client.get("/api/health/transparency").json()
    assert body["installs_7d"] == 0
    assert body["skill_error_rate_7d"] == 0.0
    assert body["skill_error_count_7d"] == 0


def test_telemetry_install_inflation_cannot_move_the_public_rate(
    transparency_client, db_session, monkeypatch
):
    """The denominator must not be client-inflatable.

    The old base was fed by ``POST /api/telemetry``, where the CALLER chooses
    ``event_type``. Since the rate is errors/base, adding base rows was a free way
    for anyone to push a public, unauthenticated quality metric toward 0 — the
    classic "make the dashboard greener by widening the denominator" lever. Here the
    write is performed THROUGH the route, and the test proves both halves: the row
    really landed (so the test cannot pass because the POST was silently rejected)
    and the published metric did not move.

    ``with_middleware=False`` only: APIKeyMiddleware is exercised by its own suite,
    and this test is about the metric, not the auth gate.
    """
    from tests._app_factory import build_test_app

    s = make_skill(db_session, slug="inflation-target")
    _installs(db_session, 10)
    for _ in range(2):
        _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR)

    import app.transparency_routes as tr

    monkeypatch.setattr(tr, "_cache_payload", None)
    monkeypatch.setattr(tr, "_cache_ts", 0.0)
    before = transparency_client.get("/api/health/transparency").json()
    assert (before["skill_error_count_7d"], before["installs_7d"], before["skill_error_rate_7d"]) == (
        2,
        10,
        pytest.approx(0.2),
    )

    # The attack: flood the old denominator through the public route.
    inflating_app = build_test_app(db_session=db_session, monkeypatch=monkeypatch, with_middleware=False)
    with TestClient(inflating_app, raise_server_exceptions=True) as c:
        for _ in range(500):
            r = c.post("/api/telemetry", json={"event_type": "install", "skill_slug": "inflation-target"})
            assert r.status_code == 201, r.text

    # The write really happened — otherwise the assertion below proves nothing.
    planted = db_session.query(TelemetryEvent).filter(TelemetryEvent.event_type == "install").count()
    assert planted == 500

    monkeypatch.setattr(tr, "_cache_payload", None)
    monkeypatch.setattr(tr, "_cache_ts", 0.0)
    after = transparency_client.get("/api/health/transparency").json()
    assert after["installs_7d"] == 10, "a client-written telemetry row moved the base"
    assert after["skill_error_rate_7d"] == pytest.approx(0.2), (
        "a client inflated the denominator of a public metric"
    )


def test_payload_reads_install_events_exactly_once(transparency_client, db_session, monkeypatch):
    """The absolute and its quotient must come from ONE read of the base.

    Mirrors ``test_payload_reads_incident_reports_exactly_once`` for the other side
    of the fraction. If ``_build_payload`` called ``_installs_7d`` twice, a row
    landing in between would publish an ``installs_7d`` the published rate was not
    computed against — the exact inconsistency the payload's own comment forbids.
    """
    from app import transparency_routes as tr

    calls: list[int] = []
    real = tr._installs_7d

    def spy(db):
        result = real(db)
        calls.append(result)
        return result

    monkeypatch.setattr(tr, "_installs_7d", spy)
    monkeypatch.setattr(tr, "_cache_payload", None)
    monkeypatch.setattr(tr, "_cache_ts", 0.0)

    s = make_skill(db_session, slug="single-base-read")
    _installs(db_session, 4)
    _row(db_session, s, report_type=IncidentReport.TYPE_SKILL_ERROR)

    r = transparency_client.get("/api/health/transparency")
    assert r.status_code == 200, r.text
    assert len(calls) == 1, f"_build_payload read install_events {len(calls)}x, expected 1"
    body = r.json()
    assert calls[0] == 4 == body["installs_7d"]
    assert body["skill_error_rate_7d"] == pytest.approx(0.25)


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
