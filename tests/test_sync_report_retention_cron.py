"""t_28f34a8a — the 30-day retention prune must be REACHABLE, not just correct.

The bug this file exists to prevent is one that every pre-existing test in
``tests/test_activate0701_sync_report.py`` was incapable of seeing: ``prune_raw``
was unit-tested by calling it directly, so it stayed green for 4½ months while
nothing in production could reach it (137 days of raw ``loop_runs`` +
``cron_health_snapshots`` retained; ``loop_run_daily_rollups`` at 2 rows for 46
raw day-groups). A test that calls the function is not a test that the function
runs.

So the priority order here is deliberate:

1. reachability of the wired entry point (CLI ``main()``, then the admin route);
2. fail-closed behaviour when the rollup that makes the delete safe is missing;
3. the invariants the delete depends on (day-alignment, rollups untouched);
4. the mechanism itself, which was already covered elsewhere.

Each guard is mutation-proven by hand before merge: revert the guard, watch the
specific test below go red with the predicted number, restore.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.crons.sync_report_pruner import CRONTAB_LINE, main, prune
from app.models import CronHealthSnapshot, LoopRun, LoopRunDailyRollup
from app.services.sync_report import (
    days_with_raw_rows,
    retention_cutoff,
    rollup_loop_runs,
    uncovered_day_groups,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CRON_SRC = REPO_ROOT / "app" / "crons" / "sync_report_pruner.py"
DEPLOY_LINE_FILE = REPO_ROOT / "deploy" / "crontab.sync-report-retention"


@pytest.fixture
def cron_cli(db_session, monkeypatch):
    """Point the cron's SessionLocal at the test session, so ``main()`` — the
    exact callable cron invokes — runs against the seeded DB instead of prod."""

    class _Shared:
        """Proxy that keeps ``close()`` a no-op: ``main()`` closes the session it
        opens, and closing the shared fixture session would detach the test from
        its own data. Same wrapper idea as ``tests/_app_factory.py``."""

        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        def close(self):  # noqa: D102 — deliberate no-op
            return None

    monkeypatch.setattr("app.database.SessionLocal", lambda: _Shared(db_session), raising=False)
    return db_session


def _seed(
    db,
    *,
    age_days: int,
    loop_slug: str = "daily-standup",
    member_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
    n: int = 1,
    is_synthetic: bool = True,
    at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert ``n`` loop_runs + one cron_health snapshot ``age_days`` old."""
    member_id = member_id or uuid.uuid4()
    fleet_id = fleet_id or uuid.uuid4()
    stamp = at or (datetime.now(UTC) - timedelta(days=age_days))
    for _ in range(n):
        db.add(
            LoopRun(
                member_id=member_id,
                fleet_id=fleet_id,
                loop_slug=loop_slug,
                instance_key=f"i-{uuid.uuid4().hex[:8]}",
                outcome="success",
                is_synthetic=is_synthetic,
                accepted_change=False,
                cost_usd=None,
                duration_seconds=12,
                created_at=stamp,
            )
        )
    db.add(
        CronHealthSnapshot(
            member_id=member_id,
            fleet_id=fleet_id,
            failed=[],
            total_count=1,
            ok_count=1,
            error_count=0,
            created_at=stamp,
        )
    )
    db.commit()
    return member_id, fleet_id


def _counts(db) -> tuple[int, int, int]:
    return (
        db.query(LoopRun).count(),
        db.query(CronHealthSnapshot).count(),
        db.query(LoopRunDailyRollup).count(),
    )


def _master_client(db_session) -> TestClient:
    """Admin router with the master-key auth sentinel (``api_key_user_id is None``)."""
    from app.admin_routes import router as admin_router
    from app.database import get_db

    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):  # pragma: no cover — trivial
        request.state.api_key_user_id = None
        return await call_next(request)

    app.include_router(admin_router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


# ── 1. reachability: the wired entry point actually deletes ──────────────────


def test_cli_entry_point_reaches_the_prune(cron_cli, capsys):
    """THE card test. ``main()`` is what crontab invokes; if the prune is ever
    unwired again, this is the test that goes red — not a direct ``prune_raw``
    unit test, which stayed green the whole time the feature was dead."""
    _seed(cron_cli, age_days=40, n=3)
    _seed(cron_cli, age_days=2, n=2, loop_slug="fresh-loop")
    before = _counts(cron_cli)
    assert before[0] == 5

    rc = main([])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert rc == 0, out
    assert out["blocked"] is False
    assert out["pruned"] == {"loop_runs": 3, "cron_health_snapshots": 1}
    remaining = cron_cli.query(LoopRun).all()
    assert len(remaining) == 2
    assert {r.loop_slug for r in remaining} == {"fresh-loop"}


def test_cli_reports_per_table_delete_counts(cron_cli, capsys):
    """Observability is a card requirement: per-table counts, not one number."""
    _seed(cron_cli, age_days=45, n=4)
    _seed(cron_cli, age_days=60, n=1, loop_slug="other", member_id=uuid.uuid4())
    rc = main([])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert out["pruned"]["loop_runs"] == 5
    assert out["pruned"]["cron_health_snapshots"] == 2
    assert set(out["pruned"]) == {"loop_runs", "cron_health_snapshots"}


def test_prune_is_idempotent_second_run_deletes_nothing(cron_cli, capsys):
    _seed(cron_cli, age_days=40, n=2)
    assert main([]) == 0
    capsys.readouterr()
    rc = main([])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert out["pruned"] == {"loop_runs": 0, "cron_health_snapshots": 0}


def test_dry_run_writes_nothing_at_all(cron_cli, capsys):
    """--dry-run is the first contact with prod; it may not create rollups either,
    or 'preview' becomes a side effect that flatters the real run's safety."""
    _seed(cron_cli, age_days=40, n=3)
    rc = main(["--dry-run"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 2, out  # blocked: nothing is rolled up yet
    assert out["would_delete"] == {"loop_runs": 3, "cron_health_snapshots": 1}
    assert _counts(cron_cli) == (3, 1, 0)


# ── 2. fail-closed: no rollup → no deletion ─────────────────────────────────
#
# ``prune()`` rolls up before it checks coverage, so a healthy run can never
# reach the block path — which is what makes it safe to install against prod.
# The guard therefore earns its keep on the failure path: it exists so that a
# broken, reverted or commented-out rollup step cannot silently turn retention
# into destruction. These tests sabotage step 1 rather than seeding an
# artificial uncovered state, because "step 1 lied" is the real failure mode.


def _sabotage_rollup(monkeypatch):
    """Make the rollup step do nothing but report success — a swallowed
    exception, a reverted call, a hotfix that skipped it. Same shape, no writes."""
    monkeypatch.setattr(
        "app.crons.sync_report_pruner.rollup_loop_runs",
        lambda db, day=None: 0,
        raising=True,
    )


def test_cli_blocks_and_rows_survive_when_the_rollup_step_fails(cron_cli, capsys, monkeypatch):
    """THE safety test. Wire-only fixes pass without it; data loss also passes
    without it. If step 1 stops writing, step 3 must not delete."""
    _seed(cron_cli, age_days=40, n=3)
    _sabotage_rollup(monkeypatch)

    rc = main([])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert rc == 2, out
    assert out["blocked"] is True
    assert out["uncovered_day_groups"] == 1
    assert out["pruned"] is None
    # Rows are still there — this is a DB assertion, not a mock assertion.
    assert _counts(cron_cli) == (3, 1, 0)


def test_a_crash_in_the_rollup_aborts_before_any_delete(cron_cli, capsys, monkeypatch):
    """Non-zero exit, nothing deleted: 'the cron errored' must never mean
    'the cron errored partway through a delete'."""

    def _boom(db, day=None):
        raise RuntimeError("rollup exploded")

    monkeypatch.setattr("app.crons.sync_report_pruner.rollup_loop_runs", _boom, raising=True)
    _seed(cron_cli, age_days=40, n=3)

    assert main([]) == 1
    assert _counts(cron_cli) == (3, 1, 0)
    capsys.readouterr()


def test_cli_rolls_up_then_prunes_and_keeps_the_aggregate(cron_cli, capsys):
    """rollup → guard → delete, in one entry point, and the number survives."""
    member_id, fleet_id = _seed(cron_cli, age_days=40, n=7)

    rc = main([])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert rc == 0, out
    assert out["rollup"] == {"days": 1, "rollup_rows": 1}
    assert _counts(cron_cli)[:2] == (0, 0)

    rollups = cron_cli.query(LoopRunDailyRollup).all()
    assert len(rollups) == 1
    row = rollups[0]
    assert (row.fleet_id, row.member_id, row.loop_slug) == (fleet_id, member_id, "daily-standup")
    assert row.runs == 7
    assert row.synthetic_runs == 7  # the mesh_0408 W4 split survives into the rollup
    assert row.day == (datetime.now(UTC) - timedelta(days=40)).date()


def test_allow_unrolled_override_is_real_not_decorative(cron_cli, capsys, monkeypatch):
    """The escape hatch must actually escape, or it is a lie operators will trust.
    Also documents what it costs: raw rows gone, no rollup behind them."""
    _seed(cron_cli, age_days=40, n=3)
    _sabotage_rollup(monkeypatch)
    assert main([]) == 2
    capsys.readouterr()

    rc = main(["--allow-unrolled"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert out["pruned"]["loop_runs"] == 3
    assert out["uncovered_day_groups"] == 1
    assert _counts(cron_cli) == (0, 0, 0), "deleted with no aggregate behind it — that is what the flag buys"


# ── 3. invariants the delete depends on ─────────────────────────────────────


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes, Postgres aware ones — same instant,
    different types. Assertions canonicalise instead of passing on one leg only."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_gate_no_raw_row_survives_past_the_cutoff(cron_cli):
    """Executable form of the card's gate: min(created_at) must advance past the
    30-day boundary on the cycle that runs."""
    for age in (137, 90, 45, 31, 5):
        _seed(cron_cli, age_days=age, n=1, loop_slug=f"l{age}", member_id=uuid.uuid4())

    assert main([]) == 0

    oldest = cron_cli.query(LoopRun.created_at).order_by(LoopRun.created_at).first()
    assert oldest is not None
    assert _utc(oldest[0]) >= retention_cutoff(30), f"min(created_at)={oldest[0]} stayed behind the cutoff"
    kept = {r.loop_slug for r in cron_cli.query(LoopRun).all()}
    assert kept == {"l5"}


def test_prune_is_day_aligned_never_splits_a_rollup_day(cron_cli):
    """A wall-clock ``now - 30d`` cutoff slices the day containing it in half. The
    surviving half then gets re-aggregated alone by the next rollup and a correct
    historical number silently shrinks. Pin the day-aligned contract."""
    ref = datetime.now(UTC)
    cutoff_day = (ref - timedelta(days=30)).date()
    # 00:00:01 on the cutoff day is OLDER than wall-clock now-30d by hours, but it
    # is part of a day that is not fully expired: it must be kept.
    just_inside = datetime.combine(cutoff_day, datetime.min.time(), tzinfo=UTC) + timedelta(seconds=1)
    last_expired = datetime.combine(cutoff_day, datetime.min.time(), tzinfo=UTC) - timedelta(seconds=1)

    _seed(cron_cli, age_days=0, at=just_inside, loop_slug="keep-me")
    _seed(cron_cli, age_days=0, at=last_expired, loop_slug="drop-me")

    assert main([]) == 0
    assert {r.loop_slug for r in cron_cli.query(LoopRun).all()} == {"keep-me"}
    # ...and the day we kept has its complete aggregate, while the pruned day's
    # aggregate also survived (prune never touches rollups).
    kept_rollup = cron_cli.query(LoopRunDailyRollup).filter(LoopRunDailyRollup.day == cutoff_day).one()
    assert kept_rollup.runs == 1
    assert kept_rollup.loop_slug == "keep-me"


def test_guard_and_rollup_bucket_days_identically(cron_cli):
    """The guard certifies coverage using the rollup's own day window. If the two
    ever diverge on a UTC-midnight row, the guard green-lights a blind delete."""
    member_id, fleet_id = uuid.uuid4(), uuid.uuid4()
    day = (datetime.now(UTC) - timedelta(days=40)).date()
    just_before = datetime.combine(day, datetime.min.time(), tzinfo=UTC) - timedelta(seconds=1)
    exactly_at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)

    _seed(cron_cli, age_days=0, at=just_before, member_id=member_id, fleet_id=fleet_id, loop_slug="late")
    _seed(cron_cli, age_days=0, at=exactly_at, member_id=member_id, fleet_id=fleet_id, loop_slug="early")

    uncovered = uncovered_day_groups(cron_cli, older_than_days=30)
    assert {(g[2], g[3]) for g in uncovered} == {("late", (day - timedelta(days=1))), ("early", day)}
    assert set(days_with_raw_rows(cron_cli)) == {day - timedelta(days=1), day}

    result = prune(db=cron_cli, older_than_days=30)
    assert result["blocked"] is False
    assert result["pruned"]["loop_runs"] == 2
    # Each row landed in the bucket its own timestamp says it belongs to — the
    # 23:59:59 row is NOT part of ``day`` just because it arrived 1s before it.
    rows = {r.day: r for r in cron_cli.query(LoopRunDailyRollup).all()}
    assert set(rows) == {day - timedelta(days=1), day}
    assert rows[day - timedelta(days=1)].loop_slug == "late"
    assert rows[day].loop_slug == "early"


def test_prune_never_touches_rollups_even_when_it_deletes_everything(cron_cli):
    """Card item 3: adoption numbers older than 30d exist ONLY in the rollups."""
    member_id, fleet_id = _seed(cron_cli, age_days=100, n=2)
    assert main([]) == 0
    before = cron_cli.query(LoopRunDailyRollup).one()
    snapshot = (before.runs, before.synthetic_runs, before.day)

    _seed(cron_cli, age_days=120, n=5, member_id=member_id, fleet_id=fleet_id, loop_slug="ancient")
    assert main([]) == 0

    rows = cron_cli.query(LoopRunDailyRollup).all()
    assert len(rows) == 2  # the 100d aggregate survived a later prune
    assert (rows[0].runs, rows[0].synthetic_runs, rows[0].day) == snapshot


def test_rollup_of_an_already_pruned_day_is_a_no_op(db_session):
    """Second data-loss path found on this card: rollup is delete-then-insert, so
    re-aggregating a pruned day used to DELETE the rollup and insert nothing back.
    Reachable by one admin POST with a historical ``{"day": ...}``."""
    member_id, fleet_id = uuid.uuid4(), uuid.uuid4()
    day = (datetime.now(UTC) - timedelta(days=120)).date()
    db_session.add(
        LoopRunDailyRollup(
            fleet_id=fleet_id,
            member_id=member_id,
            loop_slug="gone",
            day=day,
            runs=99,
            synthetic_runs=99,
            successes=99,
            failures=0,
            accepted_changes=4,
        )
    )
    db_session.commit()

    assert rollup_loop_runs(db_session, day=day) == 0

    row = db_session.query(LoopRunDailyRollup).one()
    assert (row.runs, row.accepted_changes) == (99, 4), "rollup was destroyed by re-aggregation"


def test_rollup_re_runs_are_stable(db_session):
    member_id, fleet_id = _seed(db_session, age_days=10, n=6)
    day = (datetime.now(UTC) - timedelta(days=10)).date()
    first = rollup_loop_runs(db_session, day=day)
    second = rollup_loop_runs(db_session, day=day)
    assert (first, second) == (1, 1)
    rows = (
        db_session.query(LoopRunDailyRollup)
        .filter(LoopRunDailyRollup.member_id == member_id, LoopRunDailyRollup.fleet_id == fleet_id)
        .all()
    )
    assert len(rows) == 1 and rows[0].runs == 6


def test_days_with_raw_rows_loudly_refuses_a_runaway_history(db_session):
    """The scan bound is a canary for 'retention has been dead for a year'."""
    for age in (40, 41, 42, 43, 44):
        _seed(db_session, age_days=age, n=1, loop_slug=f"l{age}", member_id=uuid.uuid4())
    with pytest.raises(RuntimeError, match="max_days=3"):
        days_with_raw_rows(db_session, max_days=3)


# ── 4. the admin surface is governed the same way ────────────────────────────


def test_admin_prune_refuses_409_and_rows_survive(db_session, monkeypatch):
    """The HTTP surface runs the same governed path as cron: if the aggregate is
    not there, the answer is 409, not a 200 with a delete count."""
    _seed(db_session, age_days=40, n=3)
    _sabotage_rollup(monkeypatch)
    client = _master_client(db_session)

    r = client.post("/api/admin/sync-report-prune", json={"days": 30})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "rollup_coverage_missing"
    assert detail["uncovered_day_groups"] == 1
    assert _counts(db_session) == (3, 1, 0)


def test_admin_rollup_route_reaches_the_rollup(db_session):
    """The sibling wiring question the card asks about: rollup_loop_runs had the
    same no-caller shape. Pin that the route actually writes rows."""
    _seed(db_session, age_days=10, n=4)
    client = _master_client(db_session)

    day = (datetime.now(UTC) - timedelta(days=10)).date()
    r = client.post("/api/admin/loop-run-rollup", json={"day": day.isoformat()})

    assert r.status_code == 200, r.text
    assert r.json() == {"rolled_up": 1, "day": day.isoformat()}
    assert db_session.query(LoopRunDailyRollup).one().runs == 4


def test_admin_prune_succeeds_once_coverage_exists(db_session):
    _seed(db_session, age_days=40, n=3)
    client = _master_client(db_session)

    r = client.post("/api/admin/sync-report-prune", json={"days": 30})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pruned"] == {"loop_runs": 3, "cron_health_snapshots": 1}
    # The route rolled the day up itself before deleting it — the delete is only
    # ever allowed behind an aggregate.
    assert body["rollup"] == {"days": 1, "rollup_rows": 1}
    assert _counts(db_session) == (0, 0, 1)


def test_admin_prune_has_no_http_override_field(db_session):
    """No ``allow_unrolled`` on the wire: the destructive override belongs on the
    CLI, where the caller already holds shell + DB credentials."""
    import app.admin_routes as ar

    src = Path(ar.__file__).read_text(encoding="utf-8")
    block = src[src.index("class PruneIn") : src.index('@router.post("/loop-run-rollup")')]
    assert "allow_unrolled" not in block
    assert re.search(r"class PruneIn\(BaseModel\):\s*\n\s*days: int = 30\s*\n", block), block


# ── 5. structural backstops: these cannot be forgotten the way a call can ────


def test_prune_raw_has_no_ungoverned_caller():
    """The inverse of this card's bug. A bare ``prune_raw(`` call outside the
    governed cron is how an unguarded DELETE gets reintroduced one refactor away."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        if path.name == "sync_report_pruner.py":  # the one governed caller
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if "prune_raw(" not in stripped or stripped.startswith(("def ", "#")):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped[:70]}")
    assert offenders == [], f"ungoverned prune_raw callers: {offenders}"


def test_cron_rolls_up_before_it_deletes():
    """Order matters more than presence: prune-then-rollup destroys the data the
    rollup is supposed to preserve. Pinned structurally, so reordering the body
    fails even if every behavioural test is fed a pre-rolled-up fixture."""
    src = CRON_SRC.read_text(encoding="utf-8")
    body = src[src.index("def prune(") : src.index("def preview(")]
    roll_at = body.index("rollup_all_days_with_raw_rows(")
    guard_at = body.index("uncovered_day_groups(")
    delete_at = body.index("prune_raw(")
    assert roll_at < guard_at < delete_at, "rollup → guard → delete is the contract"
    assert "if uncovered and not allow_unrolled" in body, "the guard must fail closed"
    assert "return 2" in src, "blocked must be distinguishable from crash in the exit code"


def test_crontab_line_and_deploy_manifest_cannot_drift():
    """Repo precedent (deploy/*.timer) is how the other pruners ended up never
    running: a schedule nobody installs. This assertion keeps the declared line
    and the installed line the same bytes."""
    assert DEPLOY_LINE_FILE.exists(), "deploy/crontab.sync-report-retention is the schedule of record"
    manifest = DEPLOY_LINE_FILE.read_text(encoding="utf-8")
    assert CRONTAB_LINE in manifest, "CRONTAB_LINE must appear verbatim in the deploy manifest"
    assert "flock -n" in CRONTAB_LINE, "no overlapping runs"
    assert "python -m app.crons.sync_report_pruner" in CRONTAB_LINE


def test_entry_point_is_importable_as_cron_invokes_it():
    """cron runs ``python -m app.crons.sync_report_pruner``; a module that cannot
    be imported, or lost ``main``, is this card's bug all over again."""
    import importlib

    mod = importlib.import_module("app.crons.sync_report_pruner")
    assert callable(mod.main)
    assert callable(mod.prune)
    assert "app.crons.sync_report_pruner" in CRONTAB_LINE


# ── 5. the ACTUAL cron command, in a real subprocess ─────────────────────────


def test_python_dash_m_is_the_wired_entry_point(tmp_path):
    """Spawn the exact command cron runs — ``python -m app.crons.sync_report_pruner``.

    Every other reachability test here calls ``main()`` in-process. That still
    passes if the ``if __name__ == "__main__": sys.exit(main())`` block is
    deleted, and a module that does nothing when invoked with ``-m`` exits 0 — so
    the cron would look healthy in the log while pruning nothing. That is
    byte-for-byte this card's bug ("written, tested, never wired"), so the proof
    has to be the subprocess cron itself.

    Runs against a throwaway SQLite file in ``tmp_path`` with a stripped
    environment: every ``*DATABASE_URL`` is removed from the inherited env and
    re-pointed at the temp file, so this can never reach prod even from a shell
    that has ``WR_DATABASE_URL`` set.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    db_file = tmp_path / "retention_e2e.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    seed_db = sessionmaker(bind=engine)()
    try:
        member, fleet = _seed(seed_db, age_days=100, n=3)  # doomed: past the 30d window
        _seed(seed_db, age_days=2, n=1, member_id=member, fleet_id=fleet)  # must survive
    finally:
        seed_db.close()
    engine.dispose()

    env = {k: v for k, v in os.environ.items() if "DATABASE_URL" not in k}
    env.update(
        {
            "WR_DATABASE_URL": url,
            "DATABASE_URL": url,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "app.crons.sync_report_pruner"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["blocked"] is False, payload
    assert payload["pruned"] == {"loop_runs": 3, "cron_health_snapshots": 1}, payload

    check = sessionmaker(bind=create_engine(url))()
    try:
        # the doomed rows are gone, the young rows survived…
        assert check.query(LoopRun).count() == 1
        assert check.query(CronHealthSnapshot).count() == 1
        # …and BOTH day-groups left a rollup behind, including the pruned one.
        # Without this, "the cron ran" and "the cron destroyed the aggregates"
        # are the same observable.
        assert check.query(LoopRunDailyRollup).count() == 2, "the pruned day must keep its aggregate"
        assert check.query(LoopRunDailyRollup).filter(LoopRunDailyRollup.runs == 3).count() == 1
    finally:
        check.close()


def test_python_dash_m_exits_2_and_keeps_rows_when_the_guard_fires(tmp_path):
    """The blocked path must be observable through the same ``-m`` command cron uses.

    ``exit 2`` is the only thing in the cron log that separates "refused to
    destroy data" from "crashed" (exit 1) and from a working run (exit 0). If
    ``main`` collapsed all three, the watchdog could not tell a guard firing from
    a silent prune — and the guard is the only thing standing between the cron
    and the 20,829 raw rows whose aggregate does not exist yet.

    The guard is provoked honestly: a ``sitecustomize`` shim on the child's
    ``PYTHONPATH`` makes the rollup step a silent no-op (it writes nothing and
    raises nothing), which is exactly the state a half-failed rollup leaves. The
    delete must then refuse.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    db_file = tmp_path / "retention_blocked.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    seed_db = sessionmaker(bind=engine)()
    try:
        _seed(seed_db, age_days=100, n=2)
    finally:
        seed_db.close()
    engine.dispose()

    # Sabotage must happen BEFORE the cron module imports the symbol, so the shim
    # patches the source module and never imports the cron module itself: doing
    # that would freeze the real binding early (and make runpy re-execute the
    # module a second time as __main__, which is what this test must not do).
    (tmp_path / "sitecustomize.py").write_text(
        "try:\n"
        "    import app.services.sync_report_retention as _r\n"
        "    _r.rollup_loop_runs = lambda db, day=None: 0  # rollup writes nothing\n"
        "except Exception as _exc:  # pragma: no cover - surfaces as a loud test failure\n"
        "    import sys\n"
        "    print(f'SHIM-FAILED: {_exc}', file=sys.stderr)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if "DATABASE_URL" not in k}
    env.update(
        {
            "WR_DATABASE_URL": url,
            "DATABASE_URL": url,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": f"{tmp_path}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "app.crons.sync_report_pruner"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "SHIM-FAILED" not in proc.stderr, f"the rollup sabotage did not load: {proc.stderr}"
    assert proc.returncode == 2, f"guard must exit 2, got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["blocked"] is True, payload
    assert payload["pruned"] is None, payload
    assert payload["uncovered_day_groups"] == 1, payload

    check = sessionmaker(bind=create_engine(url))()
    try:
        assert check.query(LoopRun).count() == 2, "a blocked prune must delete NOTHING"
        assert check.query(CronHealthSnapshot).count() == 1
    finally:
        check.close()
