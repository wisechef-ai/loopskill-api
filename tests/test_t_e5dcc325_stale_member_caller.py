"""t_e5dcc325 — the stale-member alert must be REACHABLE, not merely correct.

The defect this file exists to make impossible has now recurred three times:
``prune_raw`` was unit-tested by calling it directly and stayed green for 4½
months with nothing in production able to reach it (t_28f34a8a); the
``skill_errors`` badge answered a live dashboard with a permanent 0 because its
writer read a key no client ever built (t_a7ebcc96); ``find_stale_members`` was
the third — correct, documented as the Phase-A "failover replacement", and
imported by **nothing in app/**, so "a host went dark, do something" could never
reach an operator. Measured at audit time on prod: ``astrovita`` has been
``is_active=true`` since 2026-08-05 with no ``fleet_member_liveness`` row at all
— 114 days dark, silently.

Priority order, deliberately:

1. reachability of the wired entry points (the AST caller guard, CLI ``main()``,
   ``python -m``, the dashboard route) — this is the class of bug;
2. tenant isolation of the new per-fleet surface — the leak this wiring could
   itself have introduced;
3. the calibration repair's refusal semantics — the caller is worthless if the
   intervals it reads are wrong, and dangerous if the repair rewrites data it
   should not touch;
4. honesty of the numbers the alert reports.

Each guard was mutation-proven before merge: revert the fix, watch the named test
go red with the predicted number, restore. See the PR body for the run.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.crons.stale_member_alert import (
    CRONTAB_LINE,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED_EMPTY_SCOPE,
    EXIT_STALE_FOUND,
    main,
    sweep,
)
from app.models import APIKey, Fleet, FleetMember, FleetMemberLiveness, User
from app.services.stale_member_alert import (
    NEVER_PINGED_INTERVAL_SECONDS,
    STALE_MULTIPLE,
    count_active_members,
    find_stale_members,
)
from tests._app_factory import build_test_app

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_SRC = REPO_ROOT / "app" / "services" / "stale_member_alert.py"
DASHBOARD_SRC = REPO_ROOT / "app" / "dashboard_routes.py"
DEPLOY_MANIFEST = REPO_ROOT / "deploy" / "crontab.stale-member-alert"


# ── builders ─────────────────────────────────────────────────────────────────


def _mk_user(db, email: str) -> User:
    u = User(email=email, display_name=email.split("@")[0], subscription_tier="pro")
    db.add(u)
    db.flush()
    return u


def _mk_fleet(db, owner: User, name: str) -> Fleet:
    f = Fleet(
        owner_user_id=owner.id,
        name=name,
        # NOT NULL: the fleet's own key hash. Never a real one — these rows never
        # authenticate; the owner's user key does.
        fleet_api_key_hash=f"fh-{uuid4().hex}",
    )
    db.add(f)
    db.flush()
    return f


def _mk_member(db, fleet: Fleet, host: str) -> FleetMember:
    key = APIKey(
        user_id=fleet.owner_user_id,
        key_prefix=uuid4().hex[:8],
        key_hash=f"h-{uuid4().hex}",
        name=f"k-{host}",
    )
    db.add(key)
    db.flush()
    m = FleetMember(
        id=uuid4(),
        fleet_id=fleet.id,
        host=host,
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key.id,
    )
    db.add(m)
    db.flush()
    return m


def _ping(db, member: FleetMember, *, age_s: float | None, interval: int = 1800):
    """Give a member a liveness row. ``age_s=None`` leaves it with NO row (never pinged)."""
    if age_s is None:
        db.flush()
        return None
    lv = FleetMemberLiveness(
        member_id=member.id,
        provides={},
        reconcile_interval_seconds=interval,
        last_ping_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
    )
    db.add(lv)
    db.flush()
    return lv


class _SharedSession:
    """Proxy keeping ``close()`` a no-op: ``main()`` closes the session it opens,
    and closing the shared fixture session would detach the test from its own
    data. Same wrapper idea as ``tests/_app_factory.py``."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        return None


@pytest.fixture
def shared_session(db_session, monkeypatch):
    """Repoint ``app.database.SessionLocal`` at the test session, so the exact
    callable cron invokes runs against seeded data instead of prod."""
    monkeypatch.setattr("app.database.SessionLocal", lambda: _SharedSession(db_session), raising=False)
    return db_session


@pytest.fixture
def client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _as_owner(client, db, user: User) -> TestClient:
    """Authenticate the TestClient as the fleet owner (a user-scoped key)."""
    from app.api_key_routes import _generate_key

    plaintext, prefix, key_hash = _generate_key()
    db.add(APIKey(user_id=user.id, key_prefix=prefix, key_hash=key_hash, is_active=True))
    db.commit()
    client.headers.update({"x-api-key": plaintext})
    return client


def _seed_two_tenants(db):
    """Two tenants, each with one dark host, one of them also alive.

    Deliberately ASYMMETRIC (fleet A has 2 members, fleet B has 1): a symmetric
    seed makes a dropped filter and an inverted filter both pass, which is
    exactly how the skill_errors filter survived a mutation run.
    """
    a_owner = _mk_user(db, "tenant-a@t.com")
    a_fleet = _mk_fleet(db, a_owner, "fleet-a")
    _ping(db, _mk_member(db, a_fleet, "a-dark"), age_s=95_000)
    _ping(db, _mk_member(db, a_fleet, "a-alive"), age_s=5)

    b_owner = _mk_user(db, "tenant-b@t.com")
    b_fleet = _mk_fleet(db, b_owner, "fleet-b")
    _ping(db, _mk_member(db, b_fleet, "b-dark"), age_s=95_000)
    db.commit()
    return a_owner, a_fleet, b_owner, b_fleet


# ── 1. reachability — the class of bug this card is named for ────────────────


def _call_sites_in_app() -> list[str]:
    """AST (not substring) call sites of find_stale_members outside its own module.

    AST, because a guard that a docstring can satisfy is a guard that a
    docstring will satisfy — this card's whole history is prose promising a
    caller that did not exist.
    """
    callers: list[str] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        if path.resolve() == SERVICE_SRC.resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name == "find_stale_members":
                callers.append(str(path.relative_to(REPO_ROOT)))
    return callers


def test_find_stale_members_has_non_test_callers_in_app():
    """THE regression guard for this card's title.

    A test that imports and calls the function cannot see a missing caller —
    only a scan of app/ can. That is why 4 months of green suites coexisted with
    an alert that had never once run.
    """
    callers = _call_sites_in_app()
    assert callers, (
        "find_stale_members() has no caller in app/ — the Phase-A 'a host went dark, do "
        "something' alert is unreachable again. Wire it or retire it explicitly; do not "
        "leave it correct, documented, and uninvoked."
    )
    # Both lanes are load-bearing: push (cron) and pull (dashboard). Losing
    # either half regresses to a partial alert.
    assert "app/crons/stale_member_alert.py" in callers, f"cron sweep caller missing: {callers}"
    assert "app/dashboard_routes.py" in callers, f"dashboard pull caller missing: {callers}"


def test_cli_entry_point_reaches_the_detector(shared_session, capsys):
    """The defect was never the detector — it was that nothing ran it."""
    owner = _mk_user(shared_session, "op1@t.com")
    fleet = _mk_fleet(shared_session, owner, "f-cli")
    _ping(shared_session, _mk_member(shared_session, fleet, "dark-cli"), age_s=99_999)
    shared_session.commit()

    rc = main(["--json"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == EXIT_STALE_FOUND, "a dark host must exit 5, not 0 — see the cron module docstring"
    assert out["stale_count"] == 1
    assert out["sample"][0]["host"] == "dark-cli"
    assert out["sample"][0]["never_pinged"] is False


def test_cli_exits_zero_when_every_host_is_alive(shared_session, capsys):
    owner = _mk_user(shared_session, "op2@t.com")
    fleet = _mk_fleet(shared_session, owner, "f-ok")
    _ping(shared_session, _mk_member(shared_session, fleet, "alive"), age_s=10)
    shared_session.commit()
    assert main(["--json"]) == EXIT_OK


def test_a_sweep_that_saw_no_members_is_never_an_all_clear(shared_session, capsys):
    """HIGH-adjacent (ocr): ``stale_count == 0`` over ZERO members is not "all
    alive" — it is a typo'd --fleet-id or the wrong DATABASE_URL. Without the
    denominator every hourly run greens forever."""
    owner = _mk_user(shared_session, "empty@t.com")
    fleet = _mk_fleet(shared_session, owner, "f-empty")
    shared_session.commit()

    assert main(["--json", "--fleet-id", str(fleet.id)]) == EXIT_REFUSED_EMPTY_SCOPE
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["members_scanned"] == 0
    assert out["stale_count"] == 0, "the numbers stay honest; it is the EXIT code that refuses to green"


def test_the_denominator_counts_what_the_detector_examined(db_session):
    """One shared filter, so the count cannot drift from the sweep."""
    _seed_two_tenants(db_session)
    assert count_active_members(db_session) == 3
    fleet_a = db_session.query(Fleet).filter(Fleet.name == "fleet-a").first()
    assert count_active_members(db_session, fleet_id=fleet_a.id) == 2
    assert count_active_members(db_session, fleet_id="not-a-uuid") == 0


def test_dark_hosts_exit_code_does_not_collide_with_the_cron_contract():
    """``app/crons/_cli.py`` owns 0=ran, 1=crashed, 2=refused, 3=usage, 4=flock.
    A second meaning for 3 makes a dark host indistinguishable from a misconfigured
    job, which is how an alert gets silently muted by its own monitor."""
    import app.crons._cli as cli

    assert EXIT_STALE_FOUND not in (0, 1, 2, 3, 4), f"{EXIT_STALE_FOUND} collides with the shared contract"
    assert cli.Parser is not None
    # A typo'd flag must be usage (3), not refused (2), per the package contract.
    # Parser.error() raises SystemExit rather than returning, exactly like _cli.py's
    # own contract test — assert the code it exits with.
    with pytest.raises(SystemExit) as excinfo:
        main(["--jsonn"])
    assert excinfo.value.code == 3


def test_crashed_sweeper_is_never_read_as_an_all_clear(shared_session, monkeypatch):
    """Exit 3 (the fleet has a problem) must not collapse into exit 1 (the
    sweeper crashed). If a crash could report 0, this alert would die exactly
    the way the last one did: quietly, and looking green."""
    import app.crons.stale_member_alert as cron_mod

    def _boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(cron_mod, "find_stale_members", _boom)
    assert main([]) == EXIT_ERROR


def test_sweep_is_read_only(shared_session):
    """An alerting path that mutates fleet state creates a second, undocumented
    writer — the condition that made the old liveness column unreasonabout."""
    from sqlalchemy import text

    owner = _mk_user(shared_session, "op3@t.com")
    fleet = _mk_fleet(shared_session, owner, "f-ro")
    _ping(shared_session, _mk_member(shared_session, fleet, "dark-ro"), age_s=88_888)
    shared_session.commit()

    def counts():
        return {
            t: shared_session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("fleet_members", "fleet_member_liveness")
        }

    before = counts()
    sweep(shared_session)
    assert counts() == before


def test_sweep_separates_never_pinged_from_went_quiet(shared_session):
    """'Never wired up' and 'went dark' are different operator actions; blurring
    them into one count sends the operator to the wrong runbook."""
    owner = _mk_user(shared_session, "op4@t.com")
    fleet = _mk_fleet(shared_session, owner, "f-kind")
    _mk_member(shared_session, fleet, "never-configured")  # no liveness row at all
    _ping(shared_session, _mk_member(shared_session, fleet, "went-quiet"), age_s=77_777)
    shared_session.commit()

    report = sweep(shared_session)
    assert report["stale_count"] == 2
    assert report["never_pinged_count"] == 1
    assert report["went_quiet_count"] == 1
    assert {s["host"]: s["never_pinged"] for s in report["sample"]} == {
        "never-configured": True,
        "went-quiet": False,
    }


def test_entry_point_is_importable_exactly_as_cron_invokes_it():
    mod = importlib.import_module("app.crons.stale_member_alert")
    assert hasattr(mod, "main") and hasattr(mod, "CRONTAB_LINE")
    assert "app.crons.stale_member_alert" in CRONTAB_LINE


def test_python_dash_m_never_reports_an_all_clear_it_cannot_see(tmp_path):
    """Spawn the exact command cron runs, with no reachable fleet.

    The invariant is not a specific exit code — it is that an alert which cannot
    see the fleet must not print an empty report and exit 0. "0 stale members"
    from a sweeper that never reached the database is the most dangerous string
    this file can produce.
    """
    env = {"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "app.crons.stale_member_alert", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode != EXIT_OK, (
        f"sweeper reported 'all hosts alive' without a reachable fleet (rc=0, out={proc.stdout!r})"
    )
    assert "STALE_MEMBER_SUMMARY" not in proc.stdout + proc.stderr


def test_crontab_line_and_deploy_manifest_cannot_drift():
    assert CRONTAB_LINE in DEPLOY_MANIFEST.read_text(encoding="utf-8"), (
        "CRONTAB_LINE must appear verbatim in deploy/crontab.stale-member-alert — a "
        "schedule documented in one place and installed from another is how a cron ends "
        "up uninstalled while its code claims to be scheduled."
    )
    assert "flock -n" in CRONTAB_LINE, "no overlapping runs"
    # Package contract (app/crons/_cli.py): a lock conflict is exit 4 WITH a
    # marker, never a silent exit 1 that reads as "the sweeper crashed".
    assert "-E 4" in CRONTAB_LINE, "lock conflict must use the reserved code, not a silent 1"
    assert '{"skipped": "lock held by a live run"}' in CRONTAB_LINE, "a blocked pass must leave a trace"
    assert CRONTAB_LINE.rstrip().endswith("exit $rc"), "the real exit code must survive the marker"
    assert "python -m app.crons.stale_member_alert" in CRONTAB_LINE


# ── 2. tenant isolation of the new per-fleet surface ─────────────────────────


def test_dashboard_surfaces_stale_members(client, db_session):
    a_owner, a_fleet, _, _ = _seed_two_tenants(db_session)
    _as_owner(client, db_session, a_owner)

    r = client.get(f"/api/fleets/{a_fleet.id}/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "stale_members" in body, (
        "the key must be PRESENT — an absent key is precisely how the dead skill_errors "
        "rail looked healthy for 4.5 months"
    )
    assert body["stale_count"] == 1
    assert [s["host"] for s in body["stale_members"]] == ["a-dark"]


def test_dashboard_never_leaks_another_tenants_hosts(client, db_session):
    """The leak this wiring could itself have introduced.

    ``find_stale_members`` is unscoped by default — correct for the operator
    cron, a cross-tenant disclosure in a per-fleet response. Prod really does
    have a second client fleet (``astrovita``), so an unscoped call here would
    hand one client another client's hostnames: the same defect class codex
    caught in ``app/mcp/bundle_status.py``.
    """
    a_owner, a_fleet, _, b_fleet = _seed_two_tenants(db_session)
    _as_owner(client, db_session, a_owner)

    body = client.get(f"/api/fleets/{a_fleet.id}/dashboard").json()
    hosts = {s["host"] for s in body["stale_members"]}
    assert "b-dark" not in hosts, f"cross-tenant leak: fleet B's host reached fleet A's dashboard: {hosts}"
    assert hosts == {"a-dark"}

    b_member_ids = {
        str(m.id) for m in db_session.query(FleetMember).filter(FleetMember.fleet_id == b_fleet.id).all()
    }
    assert not ({s["member_id"] for s in body["stale_members"]} & b_member_ids), "member_ids leaked too"


def test_dashboard_caller_passes_fleet_id_structurally():
    """A route-level leak test can be left behind by a refactor; this cannot.

    Pins that the dashboard passes ``fleet_id=``, so removing the tenant
    boundary fails here even if the seeded leak test happens to stay green.
    """
    tree = ast.parse(DASHBOARD_SRC.read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", getattr(n.func, "id", None)) == "find_stale_members"
    ]
    assert calls, "the dashboard no longer calls find_stale_members — the pull lane is gone"
    for call in calls:
        assert "fleet_id" in {k.arg for k in call.keywords}, (
            "find_stale_members must be called with fleet_id= on a per-tenant route"
        )


def test_dashboard_stale_key_present_when_clean(client, db_session):
    """'No dark hosts' must stay distinguishable from 'not wired at all'."""
    owner = _mk_user(db_session, "clean@t.com")
    fleet = _mk_fleet(db_session, owner, "fleet-clean")
    _ping(db_session, _mk_member(db_session, fleet, "alive"), age_s=1)
    db_session.commit()
    _as_owner(client, db_session, owner)

    body = client.get(f"/api/fleets/{fleet.id}/dashboard").json()
    assert body["stale_members"] == []
    assert body["stale_count"] == 0


# ── 3. tenant scoping of the service itself ──────────────────────────────────


def test_unscoped_spans_fleets_and_scoped_does_not(db_session):
    _seed_two_tenants(db_session)
    assert {s.host for s in find_stale_members(db_session)} == {"a-dark", "b-dark"}
    fleet_a = db_session.query(Fleet).filter(Fleet.name == "fleet-a").first()
    assert {s.host for s in find_stale_members(db_session, fleet_id=fleet_a.id)} == {"a-dark"}


def test_scoped_call_accepts_string_and_uuid_and_refuses_garbage(db_session):
    _seed_two_tenants(db_session)
    fleet_a = db_session.query(Fleet).filter(Fleet.name == "fleet-a").first()
    by_uuid = {s.host for s in find_stale_members(db_session, fleet_id=fleet_a.id)}
    by_str = {s.host for s in find_stale_members(db_session, fleet_id=str(fleet_a.id))}
    assert by_uuid == by_str == {"a-dark"}
    # A malformed id must scope to NOTHING. Falling back to unscoped here would
    # answer a malformed tenant request with the whole deployment.
    assert find_stale_members(db_session, fleet_id="not-a-uuid") == []


def test_never_pinged_member_reports_the_alerts_own_floor(db_session):
    """The never-pinged branch reports an interval the member never declared; pin
    the constant so a future reader cannot mistake it for owner data."""
    owner = _mk_user(db_session, "np@t.com")
    fleet = _mk_fleet(db_session, owner, "f-np")
    _mk_member(db_session, fleet, "silent")
    db_session.commit()
    stale = find_stale_members(db_session, fleet_id=fleet.id)
    assert len(stale) == 1
    assert stale[0].never_pinged is True
    assert stale[0].reconcile_interval_seconds == NEVER_PINGED_INTERVAL_SECONDS


# ── 4. calibration repair — refusal semantics ────────────────────────────────


@pytest.fixture
def repair_cli(shared_session, tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.repair_fleet_liveness_intervals")
    # The ledger is a PROD audit artifact — a test run must not append rows to
    # the repo's real state/*.ledger.tsv (measured: it did, three 'test:' rows).
    monkeypatch.setattr(mod, "LEDGER_PATH", tmp_path / "fleet-liveness-repair.ledger.tsv")
    return mod


def test_repair_refuses_execute_with_no_target(repair_cli):
    """The t_d0b42dfa lesson: --execute with no target must REFUSE (exit 2) —
    not silently no-op, and not mutate every row."""
    assert repair_cli.main(["--execute", "--reason", "x"]) == 2


def test_repair_refuses_execute_without_reason(repair_cli):
    assert repair_cli.main(["--execute", "--host", "anything"]) == 2


def test_repair_dry_run_writes_nothing(repair_cli, db_session):
    owner = _mk_user(db_session, "cal@t.com")
    fleet = _mk_fleet(db_session, owner, "f-cal")
    m = _mk_member(db_session, fleet, "mis-calibrated")
    _ping(db_session, m, age_s=60, interval=300)
    db_session.commit()

    assert repair_cli.main(["--host", "mis-calibrated"]) == 0
    db_session.expire_all()
    lv = db_session.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == m.id).first()
    assert lv.reconcile_interval_seconds == 300, "a dry run mutated a row"


def test_repair_calibrates_only_the_untouched_server_default(repair_cli, db_session, capsys, tmp_path):
    """300 is the server default nobody chose; 7200 is somebody's decision.

    Seeded asymmetrically: one row safe, one row forbidden. A blanket UPDATE
    looks identical to the correct code until a declared value is in the seed.
    """
    owner = _mk_user(db_session, "cal2@t.com")
    fleet = _mk_fleet(db_session, owner, "f-cal2")
    default_row = _mk_member(db_session, fleet, "server-default")
    _ping(db_session, default_row, age_s=60, interval=300)
    declared_row = _mk_member(db_session, fleet, "owner-declared")
    _ping(db_session, declared_row, age_s=60, interval=7200)
    db_session.commit()

    rc = repair_cli.main(
        [
            "--execute",
            "--fleet-id",
            str(fleet.id),
            "--host",
            "server-default",
            "--host",
            "owner-declared",
            "--reason",
            "test: calibration",
            "--backup-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    # Contract of stdout: line 1 = the report JSON, last line = the ledger row
    # (echoed only when a row actually changed — ocr finding: the first draft
    # logged the ledger line, and `> run.json` silently swallowed it).
    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[0])["changed"] == 1
    ledger = lines[-1]
    assert "\t" in ledger and "action=update" in ledger and "table=fleet_member_liveness" in ledger, (
        "an executed repair must echo a TSV ledger row to stdout after the report"
    )
    assert (tmp_path / "fleet-liveness-repair.ledger.tsv").read_text().strip() == ledger, (
        "the echoed row and the appended row must be identical, and the append "
        "target is the ledger file — not a logger sink"
    )
    db_session.expire_all()
    got_default = (
        db_session.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == default_row.id).first()
    )
    got_declared = (
        db_session.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == declared_row.id).first()
    )
    assert got_default.reconcile_interval_seconds == 1800, (
        "server-default row must follow the collector cadence"
    )
    assert got_declared.reconcile_interval_seconds == 7200, (
        "an owner-declared interval was overwritten — the card forbids silently rewriting owner data"
    )


def test_repair_refuses_execute_with_a_host_but_no_fleet_id(repair_cli, db_session):
    """HIGH (ocr): ``FleetMember.host`` is unique only per
    ``(fleet_id, host, profile)`` — a hostname is NOT a tenant boundary, so
    executing on one can calibrate another client's rows."""
    owner = _mk_user(db_session, "xt@t.com")
    fleet = _mk_fleet(db_session, owner, "f-xt")
    m = _mk_member(db_session, fleet, "shared-hostname")
    _ping(db_session, m, age_s=60, interval=300)
    db_session.commit()

    assert (
        repair_cli.main(
            ["--execute", "--host", "shared-hostname", "--reason", "t", "--backup-dir", "/tmp/b1"]
        )
        == 2
    )
    db_session.expire_all()
    lv = db_session.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == m.id).first()
    assert lv.reconcile_interval_seconds == 300, "an unscoped host execute must not write anything"


def test_repair_refuses_execute_without_a_backup_dir(repair_cli, db_session):
    """A restore point is not optional on a prod row mutation."""
    owner = _mk_user(db_session, "nb@t.com")
    fleet = _mk_fleet(db_session, owner, "f-nb")
    m = _mk_member(db_session, fleet, "no-backup")
    _ping(db_session, m, age_s=60, interval=300)
    db_session.commit()

    assert (
        repair_cli.main(["--execute", "--member-id", str(m.id), "--reason", "t", "--fleet-id", str(fleet.id)])
        == 2
    )
    db_session.expire_all()
    lv = db_session.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == m.id).first()
    assert lv.reconcile_interval_seconds == 300


def test_repair_refuses_an_unparseable_fleet_id_rather_than_going_unscoped(repair_cli, db_session):
    """Falling back to "every fleet" on a typo is the exact failure --fleet-id
    exists to prevent."""
    assert repair_cli.main(["--execute", "--host", "x", "--fleet-id", "not-a-uuid", "--reason", "t"]) == 2


def test_repair_does_not_clobber_a_value_declared_after_the_plan(
    repair_cli, db_session, tmp_path, monkeypatch
):
    """HIGH (ocr) TOCTOU: ``loopskill_ping`` writes this same column
    concurrently. The safety verdict must be re-asserted in the UPDATE, not
    trusted from the plan read."""
    owner = _mk_user(db_session, "race@t.com")
    fleet = _mk_fleet(db_session, owner, "f-race")
    m = _mk_member(db_session, fleet, "racer")
    _ping(db_session, m, age_s=60, interval=300)
    db_session.commit()

    import app.services.stale_member_alert as _svc  # noqa: F401 — keeps module warm for the hook

    real_find = repair_cli.find_candidates

    def _find_then_owner_declares(db, **kw):
        plan = real_find(db, **kw)
        # An owner ping lands AFTER the plan read and BEFORE the write.
        for r in plan:
            lv = (
                db.query(FleetMemberLiveness)
                .filter(FleetMemberLiveness.member_id == repair_cli.UUID(r["member_id"]))
                .first()
            )
            if lv is not None:
                lv.reconcile_interval_seconds = 7200
                db.commit()
        return plan

    monkeypatch.setattr(repair_cli, "find_candidates", _find_then_owner_declares)
    rc = repair_cli.main(
        [
            "--execute",
            "--member-id",
            str(m.id),
            "--fleet-id",
            str(fleet.id),
            "--reason",
            "t",
            "--backup-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    db_session.expire_all()
    lv = db_session.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == m.id).first()
    assert lv.reconcile_interval_seconds == 7200, (
        "the guard trusted a stale plan and overwrote an owner's value"
    )


def test_repair_produces_a_verifiable_backup_written_before_the_commit(
    repair_cli, db_session, tmp_path, monkeypatch
):
    owner = _mk_user(db_session, "cal3@t.com")
    fleet = _mk_fleet(db_session, owner, "f-cal3")
    m = _mk_member(db_session, fleet, "to-fix")
    _ping(db_session, m, age_s=60, interval=300)
    db_session.commit()

    order: list[str] = []
    real_apply = repair_cli.apply_changes
    real_backup = repair_cli.write_backup

    def _backup(report, backup_dir):
        order.append("backup")
        return real_backup(report, backup_dir)

    def _apply(db, changes):
        order.append("commit")
        return real_apply(db, changes)

    monkeypatch.setattr(repair_cli, "write_backup", _backup)
    monkeypatch.setattr(repair_cli, "apply_changes", _apply)

    assert (
        repair_cli.main(
            [
                "--execute",
                "--member-id",
                str(m.id),
                "--fleet-id",
                str(fleet.id),
                "--reason",
                "t",
                "--backup-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert order == ["backup", "commit"], f"a snapshot taken after the commit is not a restore point: {order}"
    snaps = list(tmp_path.glob("fleet_member_liveness-*.json"))
    assert len(snaps) == 1, "exactly one before/after snapshot per run"
    assert json.loads(snaps[0].read_text())["rows"][0]["was"] == 300
    checksums = (tmp_path / "CHECKSUMS.txt").read_text()
    assert snaps[0].name in checksums and re.search(r"[0-9a-f]{64}", checksums), "backup must be checksummed"


def test_repair_names_targets_that_do_not_exist(repair_cli, db_session):
    """A typo'd target must be an error, not a silent 0-rows-touched success."""
    assert repair_cli.main(["--execute", "--host", "no-such-host", "--reason", "t"]) == 2


# ── 5. honesty of the reported numbers ───────────────────────────────────────


def test_threshold_uses_the_declared_interval_not_a_hardcoded_one(db_session):
    """A member declaring 7200s may go quiet for an hour; one declaring 300s may not.

    Seeds 2 dark + 1 alive (not 1+1) so a dropped filter (3) and an inverted
    filter (1) are both distinguishable from the correct answer (2).
    """
    owner = _mk_user(db_session, "thr@t.com")
    fleet = _mk_fleet(db_session, owner, "f-thr")
    _ping(db_session, _mk_member(db_session, fleet, "wide-interval-quiet"), age_s=4000, interval=7200)
    _ping(db_session, _mk_member(db_session, fleet, "narrow-interval-dark"), age_s=4000, interval=300)
    _ping(db_session, _mk_member(db_session, fleet, "very-dark"), age_s=999_999, interval=1800)
    db_session.commit()

    assert {s.host for s in find_stale_members(db_session, fleet_id=fleet.id)} == {
        "narrow-interval-dark",
        "very-dark",
    }
    assert STALE_MULTIPLE == 3


def test_sweep_emits_the_token_the_standing_predicate_greps(shared_session, caplog):
    """The prod goal predicate greps STALE_MEMBER. If the log format drops that
    token, the watch stops watching and nothing fails."""
    owner = _mk_user(shared_session, "grep@t.com")
    fleet = _mk_fleet(shared_session, owner, "f-grep")
    _ping(shared_session, _mk_member(shared_session, fleet, "dark-grep"), age_s=50_000)
    shared_session.commit()

    with caplog.at_level("WARNING"):
        main([])
    assert "STALE_MEMBER host=dark-grep" in caplog.text
    assert "STALE_MEMBER_SUMMARY" in caplog.text


# ── 6. ocr round-2 fixes: one serializer, one order, honest repair diagnoses ─


def test_both_lanes_share_one_serializer(client, db_session):
    """Push (cron report) and pull (dashboard body) entries are byte-equal.

    Two hand-built dicts drifted on day one (the pull lane dropped fleet_id);
    one payload() cannot. If someone reintroduces a per-lane dict, the key sets
    diverge and this goes red.
    """
    owner = _mk_user(db_session, "ser@t.com")
    fleet = _mk_fleet(db_session, owner, "f-ser")
    _ping(db_session, _mk_member(db_session, fleet, "ser-dark"), age_s=95_000)
    db_session.commit()

    report = sweep(db_session, fleet_id=fleet.id)
    _as_owner(client, db_session, owner)
    body = client.get(f"/api/fleets/{fleet.id}/dashboard").json()

    # Each lane evaluates with its own clock call, so the age differs by the
    # milliseconds between the reads — pin the SHAPE (the drift that matters),
    # tolerate the seconds field.
    a, b = dict(report["sample"][0]), dict(body["stale_members"][0])
    assert a.keys() == b.keys(), "the two views drifted"
    a.pop("seconds_since_ping"), b.pop("seconds_since_ping")
    assert a == b, "the two views drifted"
    assert {"member_id", "fleet_id", "host", "never_pinged"} <= set(body["stale_members"][0])


def test_both_lanes_order_by_urgency_not_db_order(client, db_session):
    """Never-pinged first (provisioning gap), then oldest ping.

    _active_member_query has no ORDER BY, so an unordered sample under
    SAMPLE_LIMIT would silently show the operator the wrong 25 hosts.
    """
    owner = _mk_user(db_session, "ord@t.com")
    fleet = _mk_fleet(db_session, owner, "f-ord")
    _ping(db_session, _mk_member(db_session, fleet, "quiet-4k"), age_s=4_000, interval=300)
    _ping(db_session, _mk_member(db_session, fleet, "silent-from-birth"), age_s=None)
    _ping(db_session, _mk_member(db_session, fleet, "ancient"), age_s=999_999, interval=1800)
    db_session.commit()

    report = sweep(db_session, fleet_id=fleet.id)
    _as_owner(client, db_session, owner)
    body = client.get(f"/api/fleets/{fleet.id}/dashboard").json()

    want = ["silent-from-birth", "ancient", "quiet-4k"]
    assert [s["host"] for s in report["sample"]] == want
    assert [s["host"] for s in body["stale_members"]] == want


def test_repair_reports_a_member_without_a_liveness_row_as_provisioning_gap(
    repair_cli, db_session, caplog, tmp_path
):
    """The astrovita class, aimed AT the repair script (ocr r2).

    An operator chasing a STALE_MEMBER never_pinged hit runs the repair on that
    host. With an INNER join it was reported as "target not found (typo,
    inactive, or outside --fleet-id)" — wrong on all three counts and pointing
    the operator at correct data. The OUTER join must land it in a distinct
    bucket with the honest diagnosis, and must NOT abort with exit 2.
    """
    owner = _mk_user(db_session, "nl@t.com")
    fleet = _mk_fleet(db_session, owner, "f-nl")
    _mk_member(db_session, fleet, "astrovita-like")  # active, NO liveness row
    db_session.commit()

    with caplog.at_level("INFO"):
        rc = repair_cli.main(
            [
                "--execute",
                "--host",
                "astrovita-like",
                "--fleet-id",
                str(fleet.id),
                "--reason",
                "t",
                "--backup-dir",
                str(tmp_path),
            ]
        )
    assert rc == 0, "a real member without a liveness row is not a typo — must not exit 2"
    assert "have NO fleet_member_liveness row" in caplog.text
    assert "not a typo" in caplog.text
    assert "targets not found" not in caplog.text, "the misleading diagnosis is back"


def test_member_id_targets_accept_uppercase_uuids(repair_cli, db_session, tmp_path):
    """A uuid pasted from a UI/log arrives uppercase; fleet-id already parses
    case-tolerantly, so member ids must too (ocr r2)."""
    owner = _mk_user(db_session, "uc@t.com")
    fleet = _mk_fleet(db_session, owner, "f-uc")
    m = _mk_member(db_session, fleet, "upper-target")
    _ping(db_session, m, age_s=60, interval=300)
    db_session.commit()

    rc = repair_cli.main(
        [
            "--execute",
            "--member-id",
            str(m.id).upper(),
            "--fleet-id",
            str(fleet.id),
            "--reason",
            "t",
            "--backup-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    db_session.expire_all()
    lv = db_session.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == m.id).first()
    assert lv.reconcile_interval_seconds == 1800, "an uppercase member id was treated as not-found"


def test_ledger_names_the_rows_it_touched(repair_cli, db_session, tmp_path):
    """The durable ledger must answer "which rows, from what to what" without
    the operator-chosen --backup-dir (ocr r2: backup dirs get pruned)."""
    owner = _mk_user(db_session, "led@t.com")
    fleet = _mk_fleet(db_session, owner, "f-led")
    m = _mk_member(db_session, fleet, "ledger-host")
    _ping(db_session, m, age_s=60, interval=300)
    db_session.commit()

    assert (
        repair_cli.main(
            [
                "--execute",
                "--member-id",
                str(m.id),
                "--fleet-id",
                str(fleet.id),
                "--reason",
                "ledger fields",
                "--backup-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    ledger = (tmp_path / "fleet-liveness-repair.ledger.tsv").read_text()
    assert f"member_ids={m.id}" in ledger, "the durable row must name the member it touched"
    assert "changes=ledger-host:300->1800" in ledger, "and the was→would transition"
