"""Unit tests for the loopskill portability CLI (cli/ package).

Run with: pytest -q cli/tests/test_loopskill_cli.py
(also runnable as tests/test_loopskill_cli.py per the parent's convention —
see the repo-root conftest.py note in the test file header below.)

Covers:
  - client discovery (present vs absent, PATH-based, never asks)
  - scanner: offline SKILL.md discovery + checksum
  - lockfile: stable/sorted/versioned JSON round-trip
  - diff: drift detection between two lockfiles, and single-file live-scan form
  - OFFLINE GUARD: the import/diff code path makes zero network calls,
    proven by breaking socket.socket for the duration of the test (not just
    asserted from a docstring)
  - apply: dry-run vs write, and IDEMPOTENCY — a second apply of the same
    pulled bundle plans zero create/update actions
  - CLI subcommand wiring end-to-end via loopskill.cli.main()
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

# The cli/ package lives outside app/, so pytest needs it on sys.path when
# run standalone. Adding cli/src is idempotent and harmless if already there
# (e.g. installed editable in the dev venv).
_CLI_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_CLI_SRC) not in sys.path:
    sys.path.insert(0, str(_CLI_SRC))

from loopskill import cli  # noqa: E402
from loopskill.apply import execute_apply, format_plan, plan_apply  # noqa: E402
from loopskill.clients import known_clients, present_clients  # noqa: E402
from loopskill.diff import diff_lockfiles, format_diff_report  # noqa: E402
from loopskill.lockfile import LOCKFILE_VERSION, build_lockfile, dumps, loads  # noqa: E402
from loopskill.pull import PulledSkill  # noqa: E402
from loopskill.scanner import scan_all, scan_client  # noqa: E402


# ─────────────────────────── fixtures ───────────────────────────


def _write_skill(root: Path, rel_dir: str, name: str, description: str, body: str = "") -> Path:
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    skill_md = d / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_md


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A fake $HOME with two clients present (claude, hermes) and two absent."""
    home = tmp_path / "home"
    home.mkdir()
    _write_skill(home, ".claude/skills/alpha", "alpha", "First skill")
    _write_skill(home, ".claude/skills/beta", "beta", "Second skill")
    _write_skill(home, ".hermes/skills/devops/gamma", "gamma", "Nested skill")
    # .codex and .cursor deliberately absent.
    return home


# ─────────────────────────── clients ───────────────────────────


def test_known_clients_includes_all_four_ids(fake_home: Path):
    ids = {c.client_id for c in known_clients(fake_home)}
    assert ids == {"claude", "hermes", "codex", "cursor"}


def test_absent_client_is_not_an_error(fake_home: Path):
    """cursor has no directory — this must be a normal state, not a crash."""
    clients = {c.client_id: c for c in known_clients(fake_home)}
    assert clients["cursor"].exists is False
    assert clients["codex"].exists is False
    assert clients["claude"].exists is True


def test_present_clients_filters_to_existing_only(fake_home: Path):
    present = {c.client_id for c in present_clients(fake_home)}
    assert present == {"claude", "hermes"}


def test_detection_is_by_path_never_by_prompting(fake_home: Path):
    """No interactive input is possible here by construction — known_clients
    takes only a Path and returns pure data, there is no input() anywhere
    in the call graph."""
    import inspect

    from loopskill import clients as clients_mod

    src = inspect.getsource(clients_mod)
    assert "input(" not in src


# ─────────────────────────── scanner ───────────────────────────


def test_scan_client_absent_root_returns_empty_list(tmp_path: Path):
    assert scan_client(tmp_path / "does-not-exist") == []


def test_scan_client_finds_skills_and_computes_checksum(fake_home: Path):
    records = scan_client(fake_home / ".claude" / "skills")
    ids = {r.skill_id for r in records}
    assert ids == {"alpha", "beta"}
    for r in records:
        assert len(r.sha256) == 64  # sha256 hex digest
        assert r.size_bytes > 0


def test_scan_client_is_deterministically_sorted(fake_home: Path):
    records = scan_client(fake_home / ".claude" / "skills")
    assert [r.skill_id for r in records] == sorted(r.skill_id for r in records)


def test_scan_all_covers_every_known_client(fake_home: Path):
    scans = scan_all(fake_home)
    by_id = {s.client.client_id: s for s in scans}
    assert len(by_id["claude"].skills) == 2
    assert len(by_id["hermes"].skills) == 1
    assert by_id["cursor"].skills == []
    assert by_id["cursor"].client.exists is False


# ─────────────────────────── lockfile ───────────────────────────


def test_lockfile_round_trip(fake_home: Path):
    scans = scan_all(fake_home)
    lockfile = build_lockfile(scans)
    assert lockfile["lockfile_version"] == LOCKFILE_VERSION
    text = dumps(lockfile)
    parsed = loads(text)
    assert parsed == lockfile


def test_lockfile_is_stable_across_repeated_builds(fake_home: Path):
    """Same inputs -> byte-identical output. This is what makes two
    machines' lockfiles genuinely diffable rather than noisy."""
    a = dumps(build_lockfile(scan_all(fake_home)))
    b = dumps(build_lockfile(scan_all(fake_home)))
    assert a == b


def test_lockfile_has_no_host_identifying_or_time_varying_fields(fake_home: Path):
    lockfile = build_lockfile(scan_all(fake_home))
    text = dumps(lockfile)
    assert "timestamp" not in text.lower()
    assert "hostname" not in text.lower()


def test_loads_rejects_non_lockfile_json():
    with pytest.raises(ValueError):
        loads(json.dumps({"unrelated": True}))


# ─────────────────────────── diff — THE DEMO ───────────────────────────


def test_diff_identical_lockfiles_reports_no_drift(fake_home: Path):
    lockfile = build_lockfile(scan_all(fake_home))
    diffs = diff_lockfiles(lockfile, lockfile)
    assert not any(d.has_drift for d in diffs)
    report = format_diff_report(diffs, label_a="a", label_b="b")
    assert "No drift" in report


def test_diff_detects_added_removed_and_changed_skills(fake_home: Path):
    lock_a = build_lockfile(scan_all(fake_home))

    # Machine B: drop 'beta', change 'alpha's content, keep 'gamma' identical.
    home_b = fake_home.parent / "home_b"
    home_b.mkdir()
    _write_skill(home_b, ".claude/skills/alpha", "alpha", "First skill — EDITED", body="new body")
    _write_skill(home_b, ".hermes/skills/devops/gamma", "gamma", "Nested skill")
    lock_b = build_lockfile(scan_all(home_b))

    diffs = {d.client_id: d for d in diff_lockfiles(lock_a, lock_b)}

    claude_diff = diffs["claude"]
    assert claude_diff.has_drift is True
    assert claude_diff.only_in_a == ["beta"]
    assert claude_diff.changed == ["alpha"]

    hermes_diff = diffs["hermes"]
    assert hermes_diff.has_drift is False
    assert hermes_diff.unchanged_count == 1


def test_diff_report_flags_drift_in_output_text(fake_home: Path):
    lock_a = build_lockfile(scan_all(fake_home))
    home_b = fake_home.parent / "home_b2"
    home_b.mkdir()
    lock_b = build_lockfile(scan_all(home_b))  # empty machine — everything is drift

    diffs = diff_lockfiles(lock_a, lock_b)
    report = format_diff_report(diffs, label_a="machine-a", label_b="machine-b")
    assert "DRIFT DETECTED" in report
    assert "DRIFT FOUND" in report


def test_diff_handles_client_present_on_only_one_side(fake_home: Path):
    """One machine has a client dir the other lacks entirely — must not crash,
    must surface as one-sided drift, not be silently skipped."""
    lock_a = build_lockfile(scan_all(fake_home))  # has .claude + .hermes
    home_b = fake_home.parent / "home_c"
    home_b.mkdir()
    _write_skill(home_b, ".codex/skills/delta", "delta", "codex-only skill")
    lock_b = build_lockfile(scan_all(home_b))

    diffs = {d.client_id: d for d in diff_lockfiles(lock_a, lock_b)}
    assert diffs["claude"].only_in_a == ["alpha", "beta"]
    assert diffs["codex"].only_in_b == ["delta"]


# ─────────────────────── OFFLINE GUARD (structural) ───────────────────────


def test_import_and_diff_make_zero_network_calls(fake_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Break socket.socket for the duration of this test. If import/diff's
    code path ever tries to open a network connection, this raises instead
    of silently succeeding via a real (or mocked-successful) HTTP call —
    proof, not a promise."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("network call attempted from an offline-only code path")

    monkeypatch.setattr(socket, "socket", _forbidden)

    # import
    scans = scan_all(fake_home)
    lockfile = build_lockfile(scans)
    text = dumps(lockfile)
    assert loads(text) == lockfile

    # diff (single-file form: lockfile vs a fresh live scan)
    diffs = diff_lockfiles(lockfile, lockfile)
    assert not any(d.has_drift for d in diffs)


def test_offline_modules_never_import_network_capable_stdlib():
    """Static guard: loopskill.clients/scanner/lockfile/diff must not import
    urllib, http, socket, or requests/httpx anywhere at module scope. This
    keeps the offline gate structural (an accidental `import urllib` in these
    files fails CI) rather than resting on runtime luck alone."""
    import ast

    forbidden = {"urllib", "http", "http.client", "socket", "requests", "httpx", "ftplib", "smtplib"}
    for modname in ("clients", "scanner", "lockfile", "diff"):
        src_path = _CLI_SRC / "loopskill" / f"{modname}.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(n.name.split(".")[0] for n in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        assert not (found & forbidden), (
            f"{modname}.py imports forbidden network module(s): {found & forbidden}"
        )


def test_pull_module_is_the_only_one_with_urllib():
    """Sanity check that the network code is actually quarantined somewhere,
    not just absent everywhere (which would make pull/apply nonfunctional)."""
    import ast

    src_path = _CLI_SRC / "loopskill" / "pull.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(n.name.split(".")[0] for n in node.names)
    assert "urllib" in found


# ─────────────────────────── apply — dry-run + idempotency ───────────────────────────


def _sample_pulled_skills() -> list[PulledSkill]:
    return [
        PulledSkill(name="alpha", content=b"# Alpha content", locked=False),
        PulledSkill(name="beta", content=b"# Beta content", locked=False),
        PulledSkill(name="gamma", content=b"", locked=True),
    ]


def test_apply_dry_run_writes_nothing(tmp_path: Path):
    dest = tmp_path / "skills"
    skills = _sample_pulled_skills()
    actions = plan_apply(skills, dest)
    report = format_plan(actions, dry_run=True)

    assert "would create" in report
    assert not dest.exists()  # nothing written — plan_apply never touches disk


def test_apply_write_creates_files_and_skips_locked(tmp_path: Path):
    dest = tmp_path / "skills"
    skills = _sample_pulled_skills()
    actions = plan_apply(skills, dest)
    execute_apply(skills, dest, actions)

    assert (dest / "alpha" / "SKILL.md").read_bytes() == b"# Alpha content"
    assert (dest / "beta" / "SKILL.md").read_bytes() == b"# Beta content"
    assert not (dest / "gamma").exists()  # locked entries are never written


def test_apply_is_idempotent_second_run_is_noop(tmp_path: Path):
    """THE ACCEPTANCE GATE: running apply --write twice in a row must plan
    (and perform) zero create/update actions on the second run."""
    dest = tmp_path / "skills"
    skills = _sample_pulled_skills()

    first_actions = plan_apply(skills, dest)
    execute_apply(skills, dest, first_actions)
    assert {a.kind for a in first_actions} == {"create", "skip-locked"}

    # Capture mtimes to prove the second run doesn't even rewrite identical bytes.
    alpha_path = dest / "alpha" / "SKILL.md"
    mtime_before = alpha_path.stat().st_mtime_ns

    second_actions = plan_apply(skills, dest)
    execute_apply(skills, dest, second_actions)

    kinds = {a.kind for a in second_actions}
    assert "create" not in kinds
    assert "update" not in kinds
    assert kinds == {"up-to-date", "skip-locked"}
    assert alpha_path.stat().st_mtime_ns == mtime_before


def test_apply_updates_changed_content_then_becomes_idempotent(tmp_path: Path):
    dest = tmp_path / "skills"
    skills = _sample_pulled_skills()
    execute_apply(skills, dest, plan_apply(skills, dest))

    changed = [
        PulledSkill(name="alpha", content=b"# Alpha content v2", locked=False),
        skills[1],
        skills[2],
    ]
    actions = plan_apply(changed, dest)
    kinds_by_name = {a.name: a.kind for a in actions}
    assert kinds_by_name["alpha"] == "update"
    assert kinds_by_name["beta"] == "up-to-date"

    execute_apply(changed, dest, actions)
    assert (dest / "alpha" / "SKILL.md").read_bytes() == b"# Alpha content v2"

    # Third run against the SAME (changed) target state is a no-op again.
    third_actions = plan_apply(changed, dest)
    assert {a.kind for a in third_actions} == {"up-to-date", "skip-locked"}


# ─────────────────────────── CLI end-to-end ───────────────────────────


def test_cli_import_writes_lockfile_to_file(fake_home: Path, tmp_path: Path):
    out = tmp_path / "out.lock.json"
    rc = cli.main(["import", "--home", str(fake_home), "-o", str(out)])
    assert rc == 0
    parsed = loads(out.read_text())
    assert parsed["clients"]["claude"]["skill_count"] == 2


def test_cli_diff_returns_zero_when_no_drift(fake_home: Path, tmp_path: Path):
    lock_path = tmp_path / "a.lock.json"
    cli.main(["import", "--home", str(fake_home), "-o", str(lock_path)])
    rc = cli.main(["diff", str(lock_path), str(lock_path)])
    assert rc == 0


def test_cli_diff_returns_one_when_drift_found(fake_home: Path, tmp_path: Path):
    lock_a = tmp_path / "a.lock.json"
    cli.main(["import", "--home", str(fake_home), "-o", str(lock_a)])

    home_b = tmp_path / "home_b"
    home_b.mkdir()
    lock_b = tmp_path / "b.lock.json"
    cli.main(["import", "--home", str(home_b), "-o", str(lock_b)])

    rc = cli.main(["diff", str(lock_a), str(lock_b)])
    assert rc == 1


def test_cli_diff_single_arg_compares_against_live_scan(fake_home: Path, tmp_path: Path, capsys):
    lock_path = tmp_path / "a.lock.json"
    cli.main(["import", "--home", str(fake_home), "-o", str(lock_path)])
    rc = cli.main(["diff", str(lock_path), "--home", str(fake_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<this machine>" in out


def test_cli_diff_missing_file_exits_cleanly(tmp_path: Path):
    with pytest.raises(SystemExit):
        cli.main(["diff", str(tmp_path / "nope.json")])


def test_cli_apply_dry_run_does_not_import_pull_eagerly():
    """The subparser wiring must not import loopskill.pull at parse time —
    only when the apply/pull handler actually runs. Guards the offline
    boundary from regressing via an incautious top-of-file import."""
    import ast

    src = (_CLI_SRC / "loopskill" / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_level_imports: set[str] = set()
    for node in tree.body:  # only TOP-LEVEL statements, not nested in functions
        if isinstance(node, ast.ImportFrom) and node.module:
            module_level_imports.add(node.module)
        elif isinstance(node, ast.Import):
            module_level_imports.update(n.name for n in node.names)
    assert "loopskill.pull" not in module_level_imports
    assert "loopskill.apply" not in module_level_imports


def test_import_with_output_reports_on_stdout(tmp_path: Path, capsys):
    """`import -o FILE` must summarise on STDOUT, not stderr.

    THE BUG THIS PINS
    -----------------
    The summary used to go to stderr unconditionally, while `diff` reports on
    stdout. README.md presents the two as ONE continuous transcript and
    promises the reader *exactly* that output — so anyone who captured or piped
    stdout (a CI step, `| tee`, a test harness) silently lost the import line
    while a plain terminal hid the problem by interleaving both streams.

    With `-o` the lockfile is written to a FILE, so stdout carries no data
    payload and the human-readable line belongs there.
    """
    home = tmp_path / "home"
    skill = home / ".claude" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nBody.\n", encoding="utf-8")
    out_path = tmp_path / "machine.lock.json"

    rc = cli.main(["import", "--home", str(home), "-o", str(out_path)])
    assert rc == 0

    captured = capsys.readouterr()
    assert "loopskill import: wrote" in captured.out, (
        "the import summary must be on stdout so the README transcript "
        f"reproduces under capture; stdout was {captured.out!r}"
    )
    assert "loopskill import: wrote" not in captured.err


def test_import_without_output_keeps_stdout_pure_json(tmp_path: Path, capsys):
    """The counterpart invariant: with NO `-o`, the lockfile JSON *is* stdout,
    so no status text may contaminate it. Guards against 'fixing' the stream
    split by moving every message to stdout unconditionally."""
    import json

    home = tmp_path / "home"
    skill = home / ".claude" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nBody.\n", encoding="utf-8")

    rc = cli.main(["import", "--home", str(home)])
    assert rc == 0

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)  # must parse — nothing else on stdout
    assert "clients" in parsed
    assert "loopskill import:" not in captured.out
