"""t_f1867996 — every job in ``app/crons/`` is either WIRED or honestly NOT WIRED.

The bug class this file kills
----------------------------
Every pruner in ``app/crons/`` documented a systemd timer (``deploy/*.timer`` +
``*.service``) as its schedule, and **none of them was ever installed**. On
wisechef-hq ``systemctl list-timers --all`` lists zero loopskill/recipes units,
and the units' ``ExecStart`` pointed at ``/opt/recipes-api`` / ``/srv/recipes-api``
prefixes that do not exist. The outcome of trusting the unit file instead of the
data: ``stripe_event_ids`` carried 13 of 15 rows past its 7-day TTL (oldest
2026-07-11) and ``fleet_pings`` 47 of 139 past 90 days (oldest 2026-07-02), while
``patch_candidates`` had never had a single row. Three jobs looked wired for
months because the thing being read was a file that described a schedule rather
than a schedule.

So this file asserts three things, in descending order of strength:

1. **Reachability through the command cron actually runs** — ``python -m
   app.crons.<job>`` in a real subprocess against a throwaway SQLite file. An
   in-process ``prune()`` test stayed green for the entire lifetime of this bug;
   ``python -m`` on a module with no ``__main__`` block exits 0 and deletes
   nothing, so the subprocess is the only honest proof.
2. **The wire cannot drift from the code** — ``CRONTAB_LINE`` must appear
   byte-identically in ``deploy/crontab.<job>``.
3. **The fiction cannot come back** — no ``*.timer`` may exist in the repo, and
   every module in ``app/crons/`` must be either wired (``CRONTAB_LINE``) or
   explicitly marked ``NOT WIRED:`` with a reason. The silent middle — a cron
   module with a docstring claiming a schedule — is what this card exists to end.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.crons import fleet_ping_pruner, stripe_event_pruner
from app.crons.fleet_ping_pruner import RetentionFloorError as FleetFloorError
from app.crons.fleet_ping_pruner import cutoff as fleet_cutoff
from app.crons.fleet_ping_pruner import prune as fleet_prune
from app.crons.fleet_ping_pruner import rows_past_ttl as fleet_rows_past_ttl
from app.crons.stripe_event_pruner import RetentionFloorError as StripeFloorError
from app.crons.stripe_event_pruner import cutoff as stripe_cutoff
from app.crons.stripe_event_pruner import prune as stripe_prune
from app.crons.stripe_event_pruner import rows_past_ttl as stripe_rows_past_ttl
from app.models import FleetPing, IncidentReport, PatchCandidate, Skill, StripeEventId

REPO_ROOT = Path(__file__).resolve().parents[1]
CRONS_DIR = REPO_ROOT / "app" / "crons"
DEPLOY_DIR = REPO_ROOT / "deploy"

# The four jobs that run from the wisechef user crontab, plus the manifest each
# must be mirrored in (sync_report_pruner arrived with t_28f34a8a / PR #476).
WIRED = {
    "stripe_event_pruner": "crontab.stripe-event-pruner",
    "fleet_ping_pruner": "crontab.fleet-ping-pruner",
    "incident_clustering": "crontab.incident-clustering",
    "sync_report_pruner": "crontab.sync-report-retention",
    "stale_member_alert": "crontab.stale-member-alert",
}
# The two jobs deliberately left unscheduled — see their docstrings. They must
# say why, in the module, rather than ship a unit file that implies otherwise.
NOT_WIRED = {"patch_drafter", "version_staleness"}


# ── helpers ────────────────────────────────────────────────────────────────


def _crontab_line(module: str) -> str:
    """Read CRONTAB_LINE via AST — no import, so a broken module cannot make a
    wiring test skip itself into a pass."""
    tree = ast.parse((CRONS_DIR / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CRONTAB_LINE":
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{module}.py defines no module-level CRONTAB_LINE")


def _manifest_line(manifest: str) -> str:
    text = (DEPLOY_DIR / manifest).read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 1, f"{manifest} must contain exactly one crontab line, got {len(lines)}"
    return lines[0]


def _cron_env(tmp_path: Path, url: str, **extra: str) -> dict[str, str]:
    """Environment for a cron subprocess.

    Every ``*DATABASE_URL`` is stripped from the inherited env and re-pointed at
    the throwaway file: a test that inherited ``WR_DATABASE_URL`` from a dev
    shell would run a real ``DELETE`` against prod.
    """
    env = {k: v for k, v in os.environ.items() if "DATABASE_URL" not in k}
    env.update({"WR_DATABASE_URL": url, "DATABASE_URL": url, "PYTHONDONTWRITEBYTECODE": "1", **extra})
    return env


def _run_cron(module: str, args: list[str], url: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", f"app.crons.{module}", *args],
        cwd=REPO_ROOT,
        env=_cron_env(tmp_path, url),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _sqlite_url(tmp_path: Path, name: str) -> str:
    from sqlalchemy import create_engine

    from app.models import Base

    path = tmp_path / f"{name}.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _seed_stripe(url: str, *, old: int, fresh: int) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    now = datetime.now(UTC)
    try:
        for i in range(old):
            session.add(
                StripeEventId(
                    event_id=f"evt_old_{i}",
                    event_type="invoice.paid",
                    processed_at=now - timedelta(days=stripe_event_pruner.TTL_DAYS + 30, hours=i),
                )
            )
        for i in range(fresh):
            session.add(
                StripeEventId(
                    event_id=f"evt_fresh_{i}",
                    event_type="invoice.paid",
                    processed_at=now - timedelta(days=1, hours=i),
                )
            )
        session.commit()
    finally:
        session.close()


def _count_stripe(url: str) -> set:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    try:
        return {r.event_id for r in session.query(StripeEventId).all()}
    finally:
        session.close()


def _seed_pings(url: str, *, old: int, fresh: int) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    today = date.today()
    try:
        for i in range(old):
            session.add(
                FleetPing(
                    salt_hash=f"old-{i}".encode(),
                    last_seen_day=today - timedelta(days=fleet_ping_pruner.TTL_DAYS + 20 + i),
                )
            )
        for i in range(fresh):
            session.add(FleetPing(salt_hash=f"fresh-{i}".encode(), last_seen_day=today - timedelta(days=i)))
        session.commit()
    finally:
        session.close()


def _count_pings(url: str) -> tuple:
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    try:
        return session.query(FleetPing).count(), session.query(func.min(FleetPing.last_seen_day)).scalar()
    finally:
        session.close()


def _seed_cluster(
    url: str,
    *,
    reports: int = 3,
    agents: int = 3,
    age_hours: int = 1,
    report_type: str = IncidentReport.TYPE_SKILL_ERROR,
    sig: str = "e" * 64,
    agent_prefix: str = "agent",
    skill_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Seed ``reports`` rows across ``agents`` distinct agents for one (skill, signature).

    ``report_type`` defaults to ``skill_error`` because that is the ONLY
    population :func:`find_clusters` clusters — see the provenance filter in
    ``app/crons/incident_clustering.py``. Seeding the server default
    (``incident``) instead would mean the "qualifying incidents in, a
    patch_candidate out" proof runs entirely on crash-shaped rows and the
    filter could be deleted without a single test noticing.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    if skill_id is None:
        skill_id = uuid.uuid4()
    try:
        if session.get(Skill, skill_id) is None:
            session.add(
                Skill(
                    id=skill_id, slug=f"cron-wire-{uuid.uuid4().hex[:6]}", title="cron wire", is_public=True
                )
            )
        occurred = datetime.now(UTC) - timedelta(hours=age_hours)
        for i in range(reports):
            session.add(
                IncidentReport(
                    id=uuid.uuid4(),
                    skill_id=skill_id,
                    error_signature=sig,
                    report_type=report_type,
                    env_fingerprint={"os": "linux"},
                    agent_fp_anon=f"{agent_prefix}-{i % agents}",
                    occurred_at=occurred,
                    command="x",
                    exit_code=1,
                    stack_trace_top="module.py:1:fn",
                )
            )
        session.commit()
    finally:
        session.close()
    return skill_id


def _candidates(url: str) -> list:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    try:
        return [(c.status, c.cluster_count, c.distinct_agents) for c in session.query(PatchCandidate).all()]
    finally:
        session.close()


# ── 1. reachability: the exact command cron runs, in a real subprocess ──────


def test_stripe_pruner_python_dash_m_actually_deletes(tmp_path):
    """THE card test for stripe_event_pruner.

    Before this card the module's only tests called ``prune()`` directly, which
    stayed green for four months while nothing could reach it. ``python -m`` is
    what the crontab invokes; deleting the ``if __name__ == "__main__"`` block
    must turn this red (verified by hand before merge).
    """
    url = _sqlite_url(tmp_path, "stripe")
    _seed_stripe(url, old=3, fresh=2)

    proc = _run_cron("stripe_event_pruner", [], url, tmp_path)
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["deleted"] == 3, payload
    assert payload["table"] == "stripe_event_ids"

    left = _count_stripe(url)
    assert len(left) == 2 and all(e.startswith("evt_fresh") for e in left), left


def test_stripe_pruner_is_idempotent_over_the_wire(tmp_path):
    """A second cron run must be a no-op, or 'the log is full of runs' means
    nothing about whether data is actually being retained."""
    url = _sqlite_url(tmp_path, "stripe2")
    _seed_stripe(url, old=4, fresh=1)
    assert (
        json.loads(_run_cron("stripe_event_pruner", [], url, tmp_path).stdout.strip().splitlines()[-1])[
            "deleted"
        ]
        == 4
    )
    second = json.loads(_run_cron("stripe_event_pruner", [], url, tmp_path).stdout.strip().splitlines()[-1])
    assert second["deleted"] == 0, second
    assert len(_count_stripe(url)) == 1


def test_stripe_pruner_dry_run_deletes_nothing(tmp_path):
    """--dry-run is first contact with prod; it may not even look like a delete."""
    url = _sqlite_url(tmp_path, "stripe_dry")
    _seed_stripe(url, old=5, fresh=2)
    proc = _run_cron("stripe_event_pruner", ["--dry-run"], url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["would_delete"] == 5 and payload["deleted"] == 0, payload
    assert len(_count_stripe(url)) == 7, "a dry run that deletes is not a dry run"


def test_stripe_pruner_refuses_below_the_replay_floor(tmp_path):
    """The 7d floor is a Stripe-replay-safety constraint, so it must be refused
    through the entry point an operator actually has — the CLI — with an exit
    code that is neither success (0) nor crash (1)."""
    url = _sqlite_url(tmp_path, "stripe_floor")
    _seed_stripe(url, old=6, fresh=1)
    proc = _run_cron("stripe_event_pruner", ["--days", "6"], url, tmp_path)
    assert proc.returncode == 2, (
        f"floor breach must exit 2, got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["blocked"] is True, payload
    assert len(_count_stripe(url)) == 7, "a refused prune must delete NOTHING"


def test_fleet_pruner_python_dash_m_actually_deletes(tmp_path):
    """Same proof for fleet_pings: 47 of 139 prod rows sat past the 90d TTL."""
    url = _sqlite_url(tmp_path, "fleet")
    _seed_pings(url, old=3, fresh=2)

    proc = _run_cron("fleet_ping_pruner", [], url, tmp_path)
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["deleted"] == 3, payload

    total, oldest = _count_pings(url)
    assert total == 2, total
    assert oldest is not None and oldest >= date.today() - timedelta(days=2), oldest


def test_fleet_pruner_refuses_below_the_product_floor(tmp_path):
    url = _sqlite_url(tmp_path, "fleet_floor")
    _seed_pings(url, old=4, fresh=1)
    proc = _run_cron("fleet_ping_pruner", ["--days", "89"], url, tmp_path)
    assert proc.returncode == 2, (
        f"floor breach must exit 2, got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    total, _ = _count_pings(url)
    assert total == 5, "a refused prune must delete NOTHING"


def test_fleet_pruner_dry_run_deletes_nothing(tmp_path):
    url = _sqlite_url(tmp_path, "fleet_dry")
    _seed_pings(url, old=3, fresh=1)
    proc = _run_cron("fleet_ping_pruner", ["--dry-run"], url, tmp_path)
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 0 and payload["would_delete"] == 3 and payload["deleted"] == 0, payload
    assert _count_pings(url)[0] == 4


def test_incident_clustering_python_dash_m_writes_a_candidate(tmp_path):
    """Clustering had never produced a row in prod (patch_candidates: 0,
    max(last_clustered_at) NULL). Reachability here means: qualifying incidents
    in, a patch_candidate out — through the command cron runs."""
    url = _sqlite_url(tmp_path, "cluster")
    _seed_cluster(url)

    proc = _run_cron("incident_clustering", [], url, tmp_path)
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["clusters"] == 1, payload
    assert _candidates(url) == [("pending", 3, 3)]

    # ...and the same command again must not duplicate the candidate.
    _run_cron("incident_clustering", [], url, tmp_path)
    assert len(_candidates(url)) == 1


def test_incident_clustering_exits_zero_when_there_is_nothing_to_do(tmp_path):
    """A quiet incident stream is a healthy state. Exiting non-zero on 0 clusters
    would page for nothing and train everyone to ignore the log."""
    url = _sqlite_url(tmp_path, "cluster_quiet")
    _seed_cluster(url, reports=2, agents=2)  # below threshold
    proc = _run_cron("incident_clustering", [], url, tmp_path)
    assert proc.returncode == 0, f"{proc.returncode}\n{proc.stderr}"
    assert json.loads(proc.stdout.strip().splitlines()[-1])["clusters"] == 0
    assert _candidates(url) == []


def test_incident_clustering_window_flag_reaches_the_query(tmp_path):
    """--hours must actually narrow the window, or the flag is decoration and
    the cron would cluster on stale incidents forever."""
    url = _sqlite_url(tmp_path, "cluster_window")
    _seed_cluster(url, age_hours=10)
    assert (
        json.loads(
            _run_cron("incident_clustering", ["--hours", "1"], url, tmp_path).stdout.strip().splitlines()[-1]
        )["clusters"]
        == 0
    )
    assert (
        json.loads(
            _run_cron("incident_clustering", ["--hours", "24"], url, tmp_path).stdout.strip().splitlines()[-1]
        )["clusters"]
        == 1
    )


# ── 2. the wire cannot drift from the code ───────────────────────────────────


@pytest.mark.parametrize("module", sorted(WIRED))
def test_crontab_line_and_deploy_manifest_are_byte_identical(module):
    """The rot this card fixes is a schedule that lives in prose. CRONTAB_LINE is
    the single source of truth; the manifest is a copy that must match exactly."""
    line = _crontab_line(module)
    manifest = DEPLOY_DIR / WIRED[module]
    assert manifest.exists(), f"{manifest} is the schedule of record"
    assert line in manifest.read_text(encoding="utf-8"), "CRONTAB_LINE must appear verbatim in the manifest"
    assert _manifest_line(WIRED[module]) == line, "the manifest must hold exactly the module's line"


@pytest.mark.parametrize("module", sorted(WIRED))
def test_crontab_line_is_a_line_cron_can_run(module):
    """Shape checks on the installed command, learned the hard way on this fleet:
    cron strips the 5 schedule fields before exec, runs with a minimal PATH, and
    has no tty — so the line must be self-contained and non-overlapping."""
    line = _crontab_line(module)
    fields = line.split()
    assert len(fields) >= 6, "5 schedule fields + a command"
    assert "cd /home/wisechef/loopskill-api" in line, "cron has no ambient cwd"
    assert ". ./.env" in line, "settings come from the env file; cron has none"
    assert "flock -n" in line, "an over-long run must never overlap the next one"
    assert f"venv/bin/python -m app.crons.{module}" in line, "must invoke the module cron claims to run"
    assert ">> /home/wisechef/wiserecipes-logs/" in line and "2>&1" in line, "stderr must land in the log too"
    assert not any(tok.startswith("~/") for tok in fields), "cron PATH has no ~ expansion for binaries"


def test_wired_schedules_do_not_stack_on_each_other():
    """Three retention jobs on one minute means the flock protects the wrong
    thing; pin the slots apart (they are also chosen clear of the 03:00/03:15/
    03:20 jobs and the 05:00 backups on wisechef-hq)."""
    slots = [tuple(_crontab_line(m).split()[:5]) for m in sorted(WIRED)]
    assert len(set(slots)) == len(slots), f"duplicate schedule fields: {slots}"


# ── 3. the fiction cannot come back ──────────────────────────────────────────


def test_no_systemd_unit_attests_to_a_cron_schedule():
    """A unit file that has never been installed is not documentation, it is a
    false attestation of a schedule — and it is what let four pruners look wired
    for months. The rot class is closed by making it un-shippable, not by a note
    in a README."""
    timers = [p for p in REPO_ROOT.rglob("*.timer") if ".git" not in p.parts]
    assert timers == [], f"systemd timers are forbidden; wire a crontab line instead: {timers}"
    stale = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in DEPLOY_DIR.glob("*.service")
        if p.name.startswith("recipes-")
    ]
    assert stale == [], f"dead recipes-*.service units must not return: {stale}"


@pytest.mark.parametrize("module", sorted(set(WIRED) | NOT_WIRED))
def test_no_cron_module_cites_a_systemd_unit_as_its_schedule(module):
    """A module may *narrate* the death of the systemd units (that history is why
    this card exists), but it may not cite one as its own schedule. So the
    needles are the specific dead unit names and unit-file directives, not the
    word "timer"."""
    src = (CRONS_DIR / f"{module}.py").read_text(encoding="utf-8")
    needles = (
        "deploy/recipes-",
        "recipes-stripe-event-pruner",
        "recipes-incident-clustering",
        "recipes-patch-drafter",
        "recipes-version-staleness",
        "ExecStart",
        "OnCalendar",
    )
    for needle in needles:
        assert needle not in src, f"{module}.py cites a deleted systemd unit ({needle!r})"


@pytest.mark.parametrize("module", sorted(WIRED))
def test_wired_module_carries_every_artifact_the_pattern_requires(module):
    """The pattern t_28f34a8a established, enforced: a wired job has CRONTAB_LINE,
    a manifest, and a ``main()`` that cron can exit-code on."""
    src = (CRONS_DIR / f"{module}.py").read_text(encoding="utf-8")
    assert "CRONTAB_LINE = (" in src
    assert 'if __name__ == "__main__":' in src, "-m on a module without this block exits 0 and does nothing"
    assert "sys.exit(main())" in src, "the cron's exit code must be main()'s, not Python's"


@pytest.mark.parametrize("module", sorted(NOT_WIRED))
def test_unwired_module_says_so_with_a_reason(module):
    """The silent middle is the bug: a cron module that neither has a schedule
    nor states that it lacks one is exactly how this card's jobs stayed dead
    while looking wired."""
    src = (CRONS_DIR / f"{module}.py").read_text(encoding="utf-8")
    assert "NOT WIRED:" in src, f"{module} must state that it is unscheduled"
    # The *assignment* is the lie; a docstring may describe the pattern to adopt
    # when the prerequisites land (both of these do).
    assert "CRONTAB_LINE = (" not in src, "an unscheduled job must not carry a fake schedule"


def test_crons_directory_is_fully_classified():
    """Every module in app/crons/ must be in exactly one of the two sets, so a
    new cron cannot land unclassified."""
    # `_`-prefixed modules are shared plumbing (app/crons/_cli.py), not jobs.
    modules = {
        p.stem for p in CRONS_DIR.glob("*.py") if p.stem not in {"__init__"} and not p.stem.startswith("_")
    }
    assert modules == WIRED.keys() | NOT_WIRED, (
        f"unclassified cron modules: {sorted(modules - (WIRED.keys() | NOT_WIRED))}; "
        "a new job needs CRONTAB_LINE + deploy/crontab.<job> + a goals predicate, "
        "or a NOT WIRED: block saying why"
    )
    for module in modules:
        src = (CRONS_DIR / f"{module}.py").read_text(encoding="utf-8")
        wired = "CRONTAB_LINE = (" in src
        honest = "NOT WIRED:" in src
        assert wired ^ honest, (
            f"{module}.py must be wired XOR explicitly NOT WIRED (got wired={wired}, honest={honest})"
        )


# ── 4. in-process guard behaviour (fast half of the same contracts) ──────────


def test_stripe_floor_is_enforced_in_the_library_not_just_the_cli(db_session):
    """A future admin route that forgets to validate still cannot breach the
    floor, because the floor lives in cutoff()/prune()."""
    assert stripe_cutoff(ttl_days=7) <= datetime.now(UTC)
    with pytest.raises(StripeFloorError, match="floor is 7d"):
        stripe_cutoff(ttl_days=6)
    db_session.add(StripeEventId(event_id="evt_x", event_type="a", processed_at=datetime.now(UTC)))
    db_session.commit()
    with pytest.raises(StripeFloorError):
        stripe_prune(db=db_session, ttl_days=0)
    assert db_session.get(StripeEventId, "evt_x") is not None


def test_fleet_floor_is_enforced_in_the_library_not_just_the_cli(db_session):
    with pytest.raises(FleetFloorError, match="floor is 90d"):
        fleet_cutoff(ttl_days=89)
    db_session.add(FleetPing(salt_hash=b"a", last_seen_day=date.today()))
    db_session.commit()
    with pytest.raises(FleetFloorError):
        fleet_prune(db=db_session, ttl_days=1)
    assert db_session.query(FleetPing).count() == 1


def test_row_counters_agree_with_the_pruners(db_session):
    """--dry-run must report exactly what the real run deletes, or the prod
    pre-flight snapshot lies about the size of the thing being destroyed."""
    now = datetime.now(UTC)
    for i in range(3):
        db_session.add(
            StripeEventId(event_id=f"e{i}", event_type="a", processed_at=now - timedelta(days=40, hours=i))
        )
    db_session.add(StripeEventId(event_id="keep", event_type="a", processed_at=now - timedelta(days=2)))
    db_session.commit()

    cutoff_at = stripe_cutoff(reference_time=now)
    assert stripe_rows_past_ttl(db_session, cutoff_at) == 3
    assert stripe_prune(reference_time=now, db=db_session) == 3
    assert stripe_rows_past_ttl(db_session, stripe_cutoff(reference_time=now)) == 0

    db_session.add(FleetPing(salt_hash=b"old", last_seen_day=date.today() - timedelta(days=200)))
    db_session.add(FleetPing(salt_hash=b"new", last_seen_day=date.today()))
    db_session.commit()
    assert fleet_rows_past_ttl(db_session, fleet_cutoff()) == 1
    assert fleet_prune(db=db_session) == 1


# ── 5. defects a real review found in this PR's own code ─────────────────────
# The pre-commit reviewer (ocr / qwen3.8-27b-heretic) found twelve issues in the
# first version of these files. Every test below pins one of them; each goes RED
# if the corresponding fix is removed. They are listed as the class of mistake a
# careful author makes while writing the guard: a floor that validates the
# REQUEST instead of the EFFECT, a cutoff computed twice, a runbook that does not
# run.


def _seed_future_pings(url: str, *, days_ahead: list[int]) -> None:
    """Rows dated in the future — reachable by any client, see _stale_condition."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    try:
        for i, ahead in enumerate(days_ahead):
            session.add(
                FleetPing(
                    salt_hash=f"future-{i}".encode(), last_seen_day=date.today() + timedelta(days=ahead)
                )
            )
        session.commit()
    finally:
        session.close()


# 5a — the floor must guard the EFFECT, not just the requested window ─────────


def test_stripe_reference_time_cannot_bypass_the_replay_floor(db_session):
    """ttl_days=7 passes the window check; a reference in 2100 still deletes
    everything, which is exactly the Stripe replay hole the floor exists to
    close. The guard therefore measures effective retention against the wall
    clock, not the parameter."""
    db_session.add_all(
        [
            StripeEventId(
                event_id=f"evt_{i}", event_type="a", processed_at=datetime.now(UTC) - timedelta(days=120)
            )
            for i in range(3)
        ]
    )
    db_session.commit()
    with pytest.raises(StripeFloorError, match="effective retention"):
        stripe_prune(db=db_session, ttl_days=7, reference_time=datetime(2100, 1, 1, tzinfo=UTC))
    assert db_session.query(StripeEventId).count() == 3, "a refused run must delete nothing"


def test_fleet_reference_day_cannot_bypass_the_product_floor(db_session):
    db_session.add_all(
        [
            FleetPing(salt_hash=f"s{i}".encode(), last_seen_day=date.today() - timedelta(days=10))
            for i in range(3)
        ]
    )
    db_session.commit()
    with pytest.raises(FleetFloorError, match="effective retention"):
        fleet_prune(db=db_session, ttl_days=90, reference_day=date(2100, 1, 1))
    assert db_session.query(FleetPing).count() == 3, "rows inside the 90d window must survive"


def test_fleet_floor_is_measured_on_the_utc_clock(monkeypatch):
    """The schedule is UTC and the reader buckets on UTC days. With date.today()
    a host whose local date is ahead of the UTC date deletes rows still inside
    the 90-day window — the destructive direction of a fail-closed guard."""
    monkeypatch.setattr(fleet_ping_pruner, "utc_today", lambda: date(2030, 1, 1))
    assert fleet_cutoff(ttl_days=90) == date(2030, 1, 1) - timedelta(days=90)


# 5b — the audit line must name the cutoff the DELETE actually used ───────────


def test_stripe_main_and_prune_share_one_reference_instant(db_session, monkeypatch):
    import app.database as database

    seen = set()
    real = stripe_event_pruner.cutoff

    def spy(**kwargs):
        seen.add(kwargs.get("reference_time"))
        return real(**kwargs)

    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(stripe_event_pruner, "cutoff", spy)
    assert stripe_event_pruner.main([]) == 0
    assert len(seen) == 1, f"main() and prune() must share ONE captured instant, saw {len(seen)}: {seen}"
    assert next(iter(seen)) is not None, "the reference must be passed explicitly, not defaulted"


def test_fleet_main_and_prune_share_one_reference_day(db_session, monkeypatch):
    import app.database as database

    seen = set()
    real = fleet_ping_pruner.cutoff

    def spy(**kwargs):
        seen.add(kwargs.get("reference_day"))
        return real(**kwargs)

    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(fleet_ping_pruner, "cutoff", spy)
    assert fleet_ping_pruner.main([]) == 0
    assert len(seen) == 1, f"main() and prune() must share ONE captured day, saw {len(seen)}: {seen}"
    assert next(iter(seen)) is not None


# 5c — future-dated rows cannot outlive the retention guarantee ───────────────


def test_fleet_pruner_removes_future_dated_pings_and_dry_run_agrees(tmp_path):
    """last_seen_day is client-supplied and unvalidated on an unauthenticated
    route, so `last_seen_day > today` is reachable by anyone. A one-sided `<`
    predicate would keep those rows forever — and fleet_liveness counts them as
    live agents while it does."""
    url = _sqlite_url(tmp_path, "fleet-future")
    _seed_pings(url, old=2, fresh=2)  # 2 rows past 90d, 2 rows inside it
    _seed_future_pings(url, days_ahead=[400, 900])
    assert _count_pings(url)[0] == 6

    dry = _run_cron("fleet_ping_pruner", ["--dry-run"], url, tmp_path)
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["would_delete"] == 4, "2 past the window + 2 from the future"
    assert _count_pings(url)[0] == 6, "dry-run must touch nothing"

    run = _run_cron("fleet_ping_pruner", [], url, tmp_path)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["deleted"] == 4, "the dry-run must predict the real delete exactly"
    assert _count_pings(url)[0] == 2, "only the in-window rows may survive"
    assert all(d <= date.today() for d in _all_ping_days(url)), "no future row may survive a prune"


def _all_ping_days(url: str) -> list:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine(url))()
    try:
        return [r.last_seen_day for r in session.query(FleetPing).all()]
    finally:
        session.close()


# 5d — usage errors must not impersonate the floor's exit 2 ───────────────────


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        ("stripe_event_pruner", ["--days", "abc"]),
        ("stripe_event_pruner", ["--nope"]),
        ("fleet_ping_pruner", ["--days", "0x10"]),
        ("incident_clustering", ["--nope"]),
        ("incident_clustering", ["--hours"]),
    ],
)
def test_usage_errors_exit_3_and_keep_json_on_stdout(module, argv, tmp_path):
    """argparse exits 2 on a usage error by default. Exit 2 here means "REFUSED
    on the retention floor" and is what an operator greps for, so a typo'd flag
    must not answer with the same code — and must not fall back to argparse's
    stderr-only usage block."""
    proc = _run_cron(module, argv, _sqlite_url(tmp_path, f"usage-{module}-{len(argv)}"), tmp_path)
    assert proc.returncode == 3, f"expected 3 (usage), got {proc.returncode}: {proc.stdout}{proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["error"].startswith("usage error:")


@pytest.mark.parametrize(
    "argv", [["--threshold", "0"], ["--threshold", "-3"], ["--hours", "0"], ["--hours", "-1"]]
)
def test_clustering_refuses_parameters_that_would_flood_the_patch_queue(argv, tmp_path):
    """`HAVING COUNT(DISTINCT agent_fp_anon) >= 0` qualifies EVERY group, so a
    zero threshold injects single-report noise into the automated patch/canary
    queue the >= 3 bar exists to keep out. A zero window instead exits 0 looking
    healthy while doing nothing. Both are refusals, not crashes."""
    url = _sqlite_url(tmp_path, "cluster-flood")
    proc = _run_cron("incident_clustering", argv, url, tmp_path)
    assert proc.returncode == 2, f"{argv}: expected 2, got {proc.returncode}: {proc.stdout}{proc.stderr}"
    assert "must be an integer >= 1" in json.loads(proc.stdout)["error"]
    assert _candidates(url) == [], "a refused clustering must mint no candidates"


# 5e — provenance: this table holds two populations and only one is a skill defect


def test_clustering_ignores_crash_reports(tmp_path):
    """incident_reports holds skill_error rows AND auto-improve crash reports
    (t_a0607d92 had to fix the same conflation in transparency_routes). The
    drafter downstream opens PRs against marketplace SKILLS, so crash reports
    must never mint a skill-level patch_candidate."""
    url = _sqlite_url(tmp_path, "cluster-crash")
    _seed_cluster(url, reports=5, agents=3, report_type=IncidentReport.TYPE_INCIDENT)
    proc = _run_cron("incident_clustering", [], url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["clusters"] == 0
    assert _candidates(url) == [], "a crash report must not become a skill patch request"


def test_clustering_cannot_cross_the_agent_bar_by_mixing_populations(tmp_path):
    """2 skill errors + 1 crash report = 3 distinct agents on one (skill,
    signature): the union clears the >= 3 bar even though neither population
    supports the cluster. The filter has to be per-population, not just
    "prefer skill errors"."""
    url = _sqlite_url(tmp_path, "cluster-mixed")
    skill = _seed_cluster(url, reports=2, agents=2, sig="a" * 64, report_type=IncidentReport.TYPE_SKILL_ERROR)
    _seed_cluster(
        url,
        reports=1,
        agents=1,
        sig="a" * 64,
        agent_prefix="crash",
        report_type=IncidentReport.TYPE_INCIDENT,
        skill_id=skill,
    )
    proc = _run_cron("incident_clustering", [], url, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["clusters"] == 0, "a mixed population must not qualify"
    assert _candidates(url) == []

    # and the same rows, all skill-shaped, DO qualify — the filter is a
    # provenance boundary, not a way of always returning nothing.
    ok = _sqlite_url(tmp_path, "cluster-pure")
    _seed_cluster(ok, reports=3, agents=3, sig="a" * 64, report_type=IncidentReport.TYPE_SKILL_ERROR)
    proc2 = _run_cron("incident_clustering", [], ok, tmp_path)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout)["clusters"] == 1
    assert len(_candidates(ok)) == 1


def test_no_opt_in_hatch_can_cluster_crash_reports(tmp_path):
    """There is deliberately NO ``--report-type incident``.

    t_0111f6c4 (PR #483, merged) settled this: ``PatchCandidate`` is keyed on
    ``(skill_id, error_signature)`` with no ``report_type`` column, so a
    crash-report cluster would collide with a skill cluster on the same pair and
    ``upsert_candidate`` would silently overwrite one population's metrics with
    the other's. A per-type lane is a schema change plus a consumer that can act
    on crash clusters — which nothing is today — not a flag. This test pins the
    ABSENCE so the escape hatch cannot creep back as a "small convenience".
    """
    import inspect

    from app.crons import incident_clustering as ic

    assert "report_type" not in inspect.signature(ic.find_clusters).parameters, (
        "find_clusters gained a report_type parameter again — readers must go "
        "through IncidentReport.skill_error_rows(), the one canonical predicate"
    )
    url = _sqlite_url(tmp_path, "cluster-crash-no-optin")
    _seed_cluster(url, reports=4, agents=3, report_type=IncidentReport.TYPE_INCIDENT)
    proc = _run_cron("incident_clustering", ["--report-type", IncidentReport.TYPE_INCIDENT], url, tmp_path)
    assert proc.returncode == 3, f"the flag must not exist; a usage error (3) is the honest answer, got {proc.returncode}"
    assert _candidates(url) == [], "no argument may turn a crash report into a skill patch request"


# 5f — the flock skip must be visible, and exit codes must stay honest ────────

SKIP_MARKED = ("stripe_event_pruner", "fleet_ping_pruner", "incident_clustering", "stale_member_alert")

# Measured on this fleet's util-linux 2.39.3: plain `flock -n LOCK cmd >> LOG 2>&1`
# exits **1 and writes zero bytes** when a live run holds the lock. flock is silent
# on EWOULDBLOCK, so a stalled hourly pass silences its own job with no trace — the
# exact silent state this card was opened to end. The line therefore carries
# `-E 4` (a conflict exit code no job uses) plus a guarded marker, proven below.
_CONFLICT_RC = 4
_SKIP_MARKER = '{"skipped": "lock held by a live run"}'


def _cronlike_command(line: str, module: str, tmp_path: Path, stub: str) -> tuple[str, Path]:
    """Turn the shipped schedule line into a command this test may really run.

    The line is rewritten ONLY where it names a host path — repo dir, log dir and
    lock file are re-pointed into ``tmp_path`` and the job is replaced by ``stub``.
    The lock/marker/exit-code machinery under test is left byte-for-byte alone.
    Re-pointing is not optional: run verbatim, this would ``cd`` into the prod
    checkout and execute the real pruner against the DB its ``.env`` names.
    """
    log = tmp_path / f"{module}.log"
    cmd = line.split(" ", 5)[5]  # cron strips the 5 schedule fields before exec
    cmd = cmd.replace("cd /home/wisechef/loopskill-api", f"cd {tmp_path}")
    cmd = cmd.replace("/home/wisechef/wiserecipes-logs", str(tmp_path))
    cmd = cmd.replace(f"/tmp/loopskill-{module.replace('_', '-')}.lock", str(tmp_path / "job.lock"))
    cmd = cmd.replace(f"venv/bin/python -m app.crons.{module}", stub)
    return cmd, log


def _sh(cmd: str, tmp_path: Path) -> subprocess.CompletedProcess:
    # cron-like minimal env: no inherited PATH tricks, bash as the shell.
    return subprocess.run(
        ["bash", "-c", cmd],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "SHELL": "/bin/bash", "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("module", SKIP_MARKED)
def test_wired_line_uses_one_flock_and_a_reserved_conflict_code(module):
    """ONE flock, not a probe-plus-run pair.

    The first draft of these lines ran `flock -n … true || echo skipped` *and then*
    `flock -n … python -m …`. Two acquisitions is a TOCTOU window, and when the
    probe won but the real call then lost the race it logged nothing — reintroducing
    the silent skip it existed to fix. A single `flock -n -E 4` cannot drift from
    itself, and 4 is outside the jobs' own 0/1/2/3 contract.
    """
    line = _crontab_line(module)
    assert line.count("flock") == 1, f"expected exactly one flock invocation, got {line.count('flock')}"
    assert "-n" in line, "must never block behind a stalled run"
    assert f"-E {_CONFLICT_RC}" in line, f"a lock conflict must be distinguishable (exit {_CONFLICT_RC})"
    assert _SKIP_MARKER in line, "a blocked pass must leave a marker in the log"
    assert line.rstrip().endswith("exit $rc"), "the real exit code must survive the marker"


@pytest.mark.parametrize("module", SKIP_MARKED)
def test_locked_out_run_leaves_a_skip_marker_and_never_executes_the_job(module, tmp_path):
    """BEHAVIOURAL, not a regex on the line: hold the lock for real, run the line.

    Asserts the three things that make a skip honest: the job's code never ran,
    the log says why, and the exit code is the reserved conflict code.
    """
    import fcntl

    stub = f"{sys.executable} -c 'print(\"JOB_EXECUTED\"); raise SystemExit(0)'"
    cmd, log = _cronlike_command(_crontab_line(module), module, tmp_path, stub)

    lock_path = tmp_path / "job.lock"
    lock_path.touch()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Positive control: prove the lock is genuinely held, else a green here
        # would only prove flock never had a chance to conflict.
        control = _sh(f"flock -n -E {_CONFLICT_RC} {lock_path} -c 'echo RACED_PAST_LOCK'", tmp_path)
        assert "RACED_PAST_LOCK" not in control.stdout, "probe invalid — the lock was never held"

        proc = _sh(cmd, tmp_path)
        assert proc.returncode == _CONFLICT_RC, f"expected exit {_CONFLICT_RC}, got {proc.returncode}: {proc}"
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        assert _SKIP_MARKER in text, f"the skip must be recorded in the log, got {text!r}"
        assert "JOB_EXECUTED" not in text, "a locked-out run must not execute the job"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.parametrize(
    ("module", "stub_rc"),
    [
        (m, rc)
        for m in SKIP_MARKED
        # 5 joins 0-3: stale_member_alert's "dark hosts found" must also pass
        # through the marker wrapper unlabelled — it is a verdict, not a skip.
        for rc in (0, 1, 2, 3, 5)
    ],
)
def test_a_run_that_started_is_never_labelled_a_skip(module, stub_rc, tmp_path):
    """A crash must not be misread as a lock skip — that would be a second
    false attestation, which is the sin this card exists to punish. Exit 1/2/3
    come from the job, so the guarded marker must stay silent and the code must
    pass through untouched.
    """
    stub = f"{sys.executable} -c 'raise SystemExit({stub_rc})'"
    cmd, log = _cronlike_command(_crontab_line(module), module, tmp_path, stub)
    proc = _sh(cmd, tmp_path)
    assert proc.returncode == stub_rc, f"job exit {stub_rc} must survive, got {proc.returncode}"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert _SKIP_MARKER not in text, f"exit {stub_rc} is not a lock conflict; log said {text!r}"


@pytest.mark.parametrize("module", SKIP_MARKED)
def test_exit_code_4_is_reserved_for_the_lock(module, tmp_path):
    """`-E 4` is only honest while no job can itself return 4. Every adversarial
    argument the CLIs refuse must land somewhere else, or a bad flag would be
    logged as "lock held" and the real cause would vanish.
    """
    for argv in ([], ["--days", "0"], ["--days", "-1"], ["--days", "abc"], ["--nope"]):
        url = f"sqlite:///{tmp_path / f'guard-{abs(hash(tuple(argv)))}.db'}"
        proc = _run_cron(module, argv, url, tmp_path)
        assert proc.returncode != _CONFLICT_RC, f"{module} {argv} claimed the reserved lock code 4"


# 5g — the manifest is an operator runbook; a broken one is worse than none ————


@pytest.mark.parametrize(("module", "manifest"), sorted(WIRED.items()))
def test_manifest_install_snippet_actually_works(module, manifest):
    """The first version of these files said
    `from app.crons.incident_clustering.py import CRONTAB_LINE` (a SyntaxError),
    `grep -v incident_clustering.py` (never matches the installed line, so every
    re-run appended a DUPLICATE schedule), and `tail …/incident_clustering.py.log`
    (a file that never appears). An idempotence guard that cannot match is not
    idempotent."""
    text = (DEPLOY_DIR / manifest).read_text(encoding="utf-8")
    line = _manifest_line(manifest)

    imported = re.search(r"from (app\.crons\.[\w.]+) import CRONTAB_LINE", text)
    assert imported, "INSTALL must show how the line is obtained from the module"
    assert imported.group(1) == f"app.crons.{module}", (
        f"install snippet imports {imported.group(1)!r}, not the dotted module app.crons.{module}"
    )
    compile(f"import {imported.group(1)}", "<manifest>", "exec")  # no `.py` suffix, real import path

    dedupe = re.search(r"crontab -l[^\n]*grep -v '?([\w.]+)'? *>", text)
    assert dedupe, "INSTALL must be idempotent (drop the old line first)"
    assert dedupe.group(1) in line, (
        f"idempotence key {dedupe.group(1)!r} never appears in the installed line — "
        "re-running the install would append a duplicate schedule"
    )

    verify = re.search(r"tail -5 (\S+)", text)
    assert verify, "VERIFY must name the log the job writes"
    log_in_line = line.rsplit(">> ", 1)[1].split(" ")[0]
    assert verify.group(1).endswith(log_in_line.lstrip("/")), (
        f"VERIFY tails {verify.group(1)!r} but the job writes {log_in_line!r}"
    )
