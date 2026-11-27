"""t_a1edaf10 — the install-count drift probe, re-homed onto the live checkout.

The probe was the ONLY writer of ``/var/lib/recipes-api/last_backfill_at`` and
its hourly cron died with the retired wiserecipes-api checkout (2026-07-12), so
``/api/health/transparency`` froze on a months-old timestamp. Re-scheduling it
naively would have been worse than leaving it dead: ``report_drift`` POSTs one
``/api/v1/skill-error`` per drifting slug and prod carries ~68 legacy drifting
rows, so the first hourly run would have opened 68 issues.

What is pinned here:

1. the BASELINE GATE  — a first run (no ledger) seeds, reports nothing;
2. the CAP            — no run reports more than ``MAX_REPORTS_PER_RUN``;
3. the DEDUPE         — a known (slug, actual, expected) is never re-reported;
4. FAIL-CLOSED        — an unwritable / corrupt ledger reports nothing;
5. the WIRE           — CRONTAB_LINE == deploy/crontab.install-count-drift-probe,
   runs from the live checkout + venv, never the retired path;
6. the PATH CONTRACT  — writer (probe) and reader (transparency) name the same
   sentinel, and the probe talks to the live API port (3370, not recipes' 3360).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.install_count_drift_probe as probe
from app.models import Skill

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "deploy" / "crontab.install-count-drift-probe"


# ── helpers ────────────────────────────────────────────────────────────────


def _mk_drifting_skill(db, slug: str, counter: int = 5) -> None:
    """A skill whose denormalised counter says ``counter`` but which has no
    install rows at all (truth 0) — i.e. a legacy drifting row."""
    db.add(
        Skill(
            id=uuid.uuid4(),
            slug=slug,
            title=slug,
            description="t",
            category="devops",
            tier="free",
            is_public=True,
            is_archived=False,
            install_count=counter,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()


class _SessionShim:
    """main() closes its session; the shared test session must stay usable."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self) -> None:  # noqa: D401 — intentionally a no-op
        return None


@pytest.fixture()
def harness(db_session, tmp_path, monkeypatch):
    """Point every probe side-effect at tmp_path and record every POST."""
    reports: list[str] = []

    def fake_report(slug, actual, expected):
        reports.append(slug)
        return True

    monkeypatch.setattr(probe, "SENTINEL", tmp_path / "last_backfill_at")
    monkeypatch.setattr(probe, "KNOWN_DRIFT_LEDGER", tmp_path / "known.json")
    monkeypatch.setattr(probe, "DEFAULT_RATE_LIMIT_STATE", tmp_path / "seen.json")
    monkeypatch.setattr(probe, "_open_session", lambda: _SessionShim(db_session))
    monkeypatch.setattr(probe, "report_drift", fake_report)
    return type("H", (), {"reports": reports, "tmp": tmp_path, "db": db_session})()


def _seed_legacy(h, n: int) -> list[str]:
    slugs = [f"legacy-{i:03d}" for i in range(n)]
    for s in slugs:
        _mk_drifting_skill(h.db, s)
    return slugs


# ── 1. baseline gate: the first run opens ZERO issues ──────────────────────


def test_first_run_with_68_legacy_drifting_rows_reports_zero(harness, caplog):
    _seed_legacy(harness, 68)
    with caplog.at_level("INFO"):
        assert probe.main() == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("reported=0 suppressed=0" in m for m in messages), messages
    assert any("68 legacy drifting slugs recorded, 0 reported" in m for m in messages), messages
    assert harness.reports == [], "the first run must seed the baseline, not file 68 issues"
    ledger = json.loads((harness.tmp / "known.json").read_text())
    assert len(ledger["known"]) == 68, "every legacy drift row is recorded as known"


def test_first_run_still_writes_the_sentinel(harness):
    """Seeding must not starve the freshness signal — that is the whole point."""
    _seed_legacy(harness, 3)
    probe.main()
    ts = (harness.tmp / "last_backfill_at").read_text()
    assert datetime.fromisoformat(ts).tzinfo is not None


def test_unwritable_sentinel_does_not_crash_and_leaves_no_fresh_timestamp(harness, monkeypatch, caplog):
    """If the sentinel cannot be written the endpoint must keep reading it as stale
    (it is the alarm), and the probe itself must still exit 0 and keep its ledger."""
    _seed_legacy(harness, 2)
    blocker = harness.tmp / "a-file-not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(
        probe, "SENTINEL", blocker / "last_backfill_at"
    )  # parent is a file: mkdir + write both fail
    with caplog.at_level("WARNING"):
        assert probe.main() == 0
    assert blocker.read_text() == "x"
    assert any("could not update sentinel" in r.getMessage() for r in caplog.records)
    assert (harness.tmp / "known.json").exists(), "the ledger is independent of the sentinel"


def test_second_run_over_unchanged_legacy_rows_reports_zero(harness):
    _seed_legacy(harness, 68)
    probe.main()
    probe.main()
    probe.main()
    assert harness.reports == []


# ── 2/3. new drift is reported once, capped, deduped ───────────────────────


def test_new_drift_after_baseline_is_reported_exactly_once(harness):
    _seed_legacy(harness, 4)
    probe.main()  # seed
    _mk_drifting_skill(harness.db, "fresh-drift")
    probe.main()
    probe.main()
    assert harness.reports == ["fresh-drift"]


def test_a_run_never_reports_more_than_the_cap(harness):
    probe.main()  # seed on an empty table → ledger exists, baseline done
    n = probe.MAX_REPORTS_PER_RUN * 3
    _seed_legacy(harness, n)
    probe.main()
    assert len(harness.reports) == probe.MAX_REPORTS_PER_RUN


def test_deferred_drift_is_picked_up_on_later_runs_not_lost(harness):
    probe.main()
    n = probe.MAX_REPORTS_PER_RUN + 2
    slugs = _seed_legacy(harness, n)
    probe.main()
    probe.main()
    assert sorted(harness.reports) == sorted(slugs), "capped slugs are deferred, never dropped"
    assert len(harness.reports) == len(set(harness.reports)), "and never reported twice"


def test_changed_drift_value_on_a_known_slug_is_reported(harness):
    _seed_legacy(harness, 1)
    probe.main()
    skill = harness.db.query(Skill).filter_by(slug="legacy-000").one()
    skill.install_count = 99
    harness.db.flush()
    probe.main()
    assert harness.reports == ["legacy-000"]


def test_resolved_drift_leaves_the_ledger_and_can_be_reported_again(harness):
    _seed_legacy(harness, 1)
    probe.main()
    skill = harness.db.query(Skill).filter_by(slug="legacy-000").one()
    skill.install_count = 0  # drift healed
    harness.db.flush()
    probe.main()
    assert json.loads((harness.tmp / "known.json").read_text())["known"] == {}
    skill.install_count = 7  # drifts again
    harness.db.flush()
    probe.main()
    assert harness.reports == ["legacy-000"]


def test_failed_report_is_retried_not_marked_known(harness, monkeypatch):
    probe.main()
    _mk_drifting_skill(harness.db, "flaky")
    monkeypatch.setattr(probe, "report_drift", lambda *a: False)
    probe.main()
    ledger = json.loads((harness.tmp / "known.json").read_text())
    assert "flaky" not in ledger["known"], "a failed POST must stay eligible for the next run"


# ── 4. fail closed ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("garbage", ["", "{not json", "[]", '"str"', '{"known": 5}', '{"known": {"a": "x"}}'])
def test_corrupt_ledger_reseeds_instead_of_flooding(harness, garbage):
    _seed_legacy(harness, 68)
    (harness.tmp / "known.json").write_text(garbage)
    probe.main()
    assert harness.reports == []


def test_unwritable_ledger_reports_nothing(harness, monkeypatch):
    """If the baseline cannot be persisted, every hourly run would look like a
    first run and re-seed forever; reporting in that state would flood. Silence
    is the safe failure."""
    _seed_legacy(harness, 68)
    monkeypatch.setattr(probe, "KNOWN_DRIFT_LEDGER", harness.tmp / "no-such-dir" / "x" / "known.json")
    monkeypatch.setattr(probe, "save_known", lambda *a, **k: False)
    probe.main()
    assert harness.reports == []


def test_ledger_that_turns_unwritable_after_baseline_reports_nothing(harness, monkeypatch, caplog):
    """Review finding on #495: the fail-closed rule held only for the baseline.
    With a ledger already on disk, a run whose ledger write fails would POST and
    then forget it did, so every later run would re-report the same slugs. The
    ledger must prove it can be written BEFORE any POST."""
    _seed_legacy(harness, 2)
    probe.main()  # baseline seeded and persisted
    _mk_drifting_skill(harness.db, "fresh-drift")
    monkeypatch.setattr(probe, "save_known", lambda *a, **k: False)
    with caplog.at_level("WARNING"):
        assert probe.main() == 0
    assert harness.reports == [], "an unpersistable ledger must suppress every POST"
    assert any("fail-closed" in r.getMessage() for r in caplog.records)


def test_ledger_survives_a_crash_halfway_write(harness, monkeypatch):
    """Atomic replace: a crash between write and rename must leave the previous
    ledger intact, never a truncated one that re-seeds."""
    _seed_legacy(harness, 3)
    probe.main()
    before = (harness.tmp / "known.json").read_text()

    def boom(*a, **k):
        raise OSError("disk full mid-replace")

    monkeypatch.setattr(os, "replace", boom)
    assert probe.save_known(harness.tmp / "known.json", {"zzz": (1, 2)}) is False
    assert (harness.tmp / "known.json").read_text() == before


def test_save_then_load_round_trips_and_reports_success(tmp_path):
    path = tmp_path / "nested" / "deeper" / "known.json"  # parents must be created
    assert probe.save_known(path, {"a": (5, 2), "b": (0, 3)}) is True
    assert probe.load_known(path) == {"a": (5, 2), "b": (0, 3)}
    assert probe.save_known(path, {"a": (6, 2)}) is True, "target already exists / dir already exists"
    assert probe.load_known(path) == {"a": (6, 2)}
    assert not list(path.parent.glob("*.tmp")), "no temp debris after a clean write"


def test_load_known_distinguishes_absent_from_empty(tmp_path):
    assert probe.load_known(tmp_path / "absent.json") is None, "absent = first run"
    (tmp_path / "empty.json").write_text(json.dumps({"known": {}}))
    assert probe.load_known(tmp_path / "empty.json") == {}, "an empty ledger is a real baseline"


def test_run_summary_counts_are_what_the_log_says(harness, monkeypatch, caplog):
    """report-only values still must not lie: total_drift is the SUM of |counter - truth|."""
    _seed_legacy(harness, 2)
    monkeypatch.setattr(probe, "compute_truth", lambda db: {"legacy-000": 2, "legacy-001": 0})
    probe.main()  # seed
    skill = harness.db.query(Skill).filter_by(slug="legacy-000").one()
    skill.install_count = 9  # 9 vs truth 2 -> changed, reportable
    harness.db.flush()
    with caplog.at_level("INFO"):
        probe.main()
    summary = [r.getMessage() for r in caplog.records if "drift probe complete" in r.getMessage()][-1]
    assert "total_drift=12" in summary, summary  # |9-2| + |5-0|
    assert "reported=1" in summary and "suppressed=0" in summary, summary


def test_rate_limit_suppression_is_counted_not_reported(harness, monkeypatch, caplog):
    probe.main()
    _mk_drifting_skill(harness.db, "limited")
    monkeypatch.setattr(probe, "should_report", lambda *a, **k: False)
    with caplog.at_level("INFO"):
        probe.main()
    assert harness.reports == []
    assert any("suppressed=1" in r.getMessage() and "reported=0" in r.getMessage() for r in caplog.records)


def _set_counter(h, slug: str, value: int) -> None:
    skill = h.db.query(Skill).filter_by(slug=slug).one()
    skill.install_count = value
    h.db.flush()


def test_changed_value_inside_the_24h_window_is_reported_not_delayed(harness):
    """Follow-up on #495: the limiter key was constant per slug, so a slug reported
    once and then drifting to a NEW (actual, expected) inside 24h was held back until
    the window passed. A different value is a different report."""
    probe.main()  # baseline (empty)
    _mk_drifting_skill(harness.db, "moving", counter=5)
    probe.main()  # reported once: (5, 0)
    _set_counter(harness, "moving", 99)  # (99, 0), same hour
    probe.main()
    assert harness.reports == ["moving", "moving"]
    ledger = json.loads((harness.tmp / "known.json").read_text())
    assert ledger["known"]["moving"] == [99, 0], "the delivered report marks the NEW value known"


def test_same_value_inside_the_24h_window_is_still_suppressed(harness, caplog):
    """Dedupe must not regress: heal then re-drift to the SAME (actual, expected)
    inside 24h sends nothing, and the slug stays out of the ledger (not lost)."""
    probe.main()
    _mk_drifting_skill(harness.db, "flapper", counter=5)
    probe.main()  # reported: (5, 0)
    _set_counter(harness, "flapper", 0)  # heals -> leaves the ledger
    probe.main()
    _set_counter(harness, "flapper", 5)  # re-drifts, identical value
    with caplog.at_level("INFO"):
        probe.main()
    assert harness.reports == ["flapper"], "identical (actual, expected) inside 24h: no duplicate issue"
    assert "flapper" not in json.loads((harness.tmp / "known.json").read_text())["known"]
    assert any("suppressed=1" in r.getMessage() for r in caplog.records)


def test_limiter_signature_is_a_pure_function_of_slug_and_both_values():
    sig = probe.limiter_signature
    assert sig("a", 5, 0) == sig("a", 5, 0)
    assert len({sig("a", 5, 0), sig("a", 6, 0), sig("a", 5, 1), sig("b", 5, 0)}) == 4
    assert sig("a", 1, 23) != sig("a", 12, 3), "no digit-boundary collision between actual and expected"
    assert sig("a", 5, 0) != probe.compute_signature("a"), (
        "the value-blind wire signature is not the limiter key"
    )


def test_wire_signature_stays_value_blind(monkeypatch):
    """The error_signature POSTed to the tracker keeps its per-slug dedupe; only the
    local limiter key learns about the values."""
    sent = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return _Resp()

    monkeypatch.setattr(probe, "WR_API_KEY", "k")
    monkeypatch.setattr(probe.urllib.request, "urlopen", fake_urlopen)
    assert probe.report_drift("s", 5, 0) and probe.report_drift("s", 9, 0)
    assert {p["error_signature"] for p in sent} == {probe.compute_signature("s")}


# ── pure planner ────────────────────────────────────────────────────────────


def test_plan_reports_is_pure_and_deterministic():
    drifts = {"b": (2, 0), "a": (1, 0), "c": (3, 0), "d": (4, 1)}
    known = {"c": (3, 0), "d": (4, 0)}
    first = probe.plan_reports(drifts, known)
    assert first == ["a", "b", "d"], "sorted; known-unchanged excluded; known-but-changed included"
    assert probe.plan_reports(drifts, known) == first
    assert known == {"c": (3, 0), "d": (4, 0)}, "inputs are not mutated"


def test_rate_limited_slugs_do_not_starve_the_cap(harness, monkeypatch):
    """A slug the 24h limiter suppresses sends nothing, so it must not use a cap slot —
    otherwise N suppressed slugs sorted first would block every new drift forever."""
    probe.main()  # seed empty
    n = probe.MAX_REPORTS_PER_RUN
    for i in range(n):
        _mk_drifting_skill(harness.db, f"aaa-limited-{i}")
    for i in range(n):
        _mk_drifting_skill(harness.db, f"zzz-new-{i}")
    monkeypatch.setattr(probe, "should_report", lambda slug, sig, **k: not slug.startswith("aaa-"))
    probe.main()
    assert sorted(harness.reports) == [f"zzz-new-{i}" for i in range(n)]


# ── 5. the wire ─────────────────────────────────────────────────────────────


def _manifest_lines() -> list[str]:
    text = MANIFEST.read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def _module_crontab_line() -> str:
    tree = ast.parse((REPO_ROOT / "scripts" / "install_count_drift_probe.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CRONTAB_LINE" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("probe defines no CRONTAB_LINE")


def test_manifest_holds_exactly_the_probe_crontab_line():
    assert _manifest_lines() == [_module_crontab_line()]
    assert probe.CRONTAB_LINE == _module_crontab_line()


def test_cron_runs_from_the_live_checkout_and_never_the_retired_path():
    line = probe.CRONTAB_LINE
    assert "cd /home/wisechef/loopskill-api" in line
    assert "venv/bin/python scripts/install_count_drift_probe.py" in line
    assert ". ./.env" in line, "WR_API_KEY + DB url come from the env file"
    assert "flock -n -E 4" in line, "runs never overlap; a lock conflict is distinguishable"
    assert line.rstrip().endswith("exit $rc")
    assert ".venv/bin/python" not in line, (
        "the failure that killed the old cron (log: .venv/bin/python: No such file)"
    )
    assert "wiserecipes-api" not in line and "recipes-api/" not in line
    assert "/wiserecipes-logs/" in line and "2>&1" in line


def test_cron_runs_hourly_on_a_minute_no_other_wired_job_uses():
    fields = probe.CRONTAB_LINE.split()[:5]
    assert fields[1:] == ["*", "*", "*", "*"] and fields[0].isdigit()
    taken = set()
    for m in (REPO_ROOT / "deploy").glob("crontab.*"):
        if m == MANIFEST:
            continue
        for ln in m.read_text(encoding="utf-8").splitlines():
            if ln.strip() and not ln.startswith("#"):
                taken.add(tuple(ln.split()[:5]))
    assert tuple(fields) not in taken


def test_manifest_install_snippet_is_idempotent_and_tails_the_right_log():
    import re

    text = MANIFEST.read_text(encoding="utf-8")
    key = re.search(r"crontab -l[^\n]*grep -v '?([\w./-]+)'? *>", text)
    assert key, "INSTALL must drop the old line first so a re-run never duplicates"
    assert key.group(1) in probe.CRONTAB_LINE
    tail = re.search(r"tail -5 (\S+)", text)
    assert tail and tail.group(1) in probe.CRONTAB_LINE


def test_a_locked_out_run_never_executes_the_probe(tmp_path):
    """BEHAVIOURAL: hold the lock for real, run the shipped line (re-pointed into
    tmp_path, probe swapped for a stub); the stub must not run and rc must be 4."""
    import fcntl

    line = probe.CRONTAB_LINE
    cmd = line.split(" ", 5)[5]
    cmd = cmd.replace("cd /home/wisechef/loopskill-api", f"cd {tmp_path}")
    cmd = cmd.replace("/home/wisechef/wiserecipes-logs", str(tmp_path))
    cmd = cmd.replace("/tmp/loopskill-install-count-drift-probe.lock", str(tmp_path / "job.lock"))
    cmd = cmd.replace(
        "venv/bin/python scripts/install_count_drift_probe.py",
        f"{sys.executable} -c 'print(\"JOB_EXECUTED\")'",
    )
    (tmp_path / ".env").write_text("")
    lock = tmp_path / "job.lock"
    lock.touch()
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", ""), "SHELL": "/bin/bash"},
            capture_output=True,
            text=True,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    log = (tmp_path / "install_count_drift_probe.log").read_text()
    assert proc.returncode == 4, proc
    assert "JOB_EXECUTED" not in log
    assert "lock held by a live run" in log


# ── 6. path contract: writer == reader, and the right API port ──────────────


def test_probe_sentinel_is_exactly_the_path_the_endpoint_reads():
    from app.transparency_routes import BACKFILL_SENTINEL_PATH

    assert str(Path(BACKFILL_SENTINEL_PATH)) == "/var/lib/recipes-api/last_backfill_at"
    src = (REPO_ROOT / "scripts" / "install_count_drift_probe.py").read_text(encoding="utf-8")
    assert f'Path("{BACKFILL_SENTINEL_PATH}")' in src, "writer and reader must name the SAME file"


def test_probe_defaults_to_the_live_api_port_not_the_recipes_port():
    env = {k: v for k, v in os.environ.items() if k != "RECIPES_API_BASE"}
    out = subprocess.check_output(
        [sys.executable, "-c", "import scripts.install_count_drift_probe as p; print(p.API_BASE)"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
    ).strip()
    assert out == "http://127.0.0.1:3370", (
        "3360 is the separate recipes product; loopskill-api listens on 3370"
    )
