"""Gate tests for scripts/ocr-gate.sh — the executable form of AGENTS.md rule 3.

CI has no ``ocr`` binary, so every case drives a stub: the gate's contract is
about what IT does with ocr's output, not about ocr's behaviour. The stubs replay
a canned preview response and record their argv to ``$STUB_ARGV``.

The pair that matters most is ``test_binary_absent_is_not_a_pass`` vs
``test_clean_tree_with_binary_passes_vacuously``: same repo, same working tree,
only the binary differs, and the two must not return the same code. Before the
gate existed, "the tool is missing" and "the diff is clean" were
indistinguishable — that is the silent-skip shape this file exists to kill.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "ocr-gate.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=gate", "-c", "user.email=gate@test", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('hi')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def make_stub(
    tmp_path: Path, *, payload=None, raw: str | None = None, exit_code: int = 0, name: str = "ocr-stub"
) -> Path:
    """A fake ``ocr`` that replays a canned preview response and logs argv."""
    stub = tmp_path / name
    if raw is None:
        raw = json.dumps(payload if payload is not None else {})
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" > "$STUB_ARGV"\n'
        f"cat <<'STUBEOF'\n{raw}\nSTUBEOF\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)
    return stub


def gate(
    repo: Path,
    tmp_path: Path,
    *,
    ocr_bin: str | None = None,
    extra: list[str] | None = None,
    ack: str | None = None,
    stub=None,
) -> subprocess.CompletedProcess:
    """Run the gate with a fully controlled environment."""
    if ocr_bin is None:
        assert stub is not None, "pass stub= or ocr_bin="
        ocr_bin = str(stub)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "no-home"),  # the host's ~/bin/ocr must stay invisible
        "TMPDIR": str(tmp_path),
        "STUB_ARGV": str(tmp_path / "argv.txt"),
        "OCR_BIN": ocr_bin,
    }
    if ack is not None:
        env["OCR_GATE_ACK"] = ack
    return subprocess.run(
        ["bash", str(GATE), "--repo", str(repo), *(extra or [])],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# --- the crux: absence vs. cleanliness must be distinguishable ---------------


def test_binary_absent_is_not_a_pass(git_repo, tmp_path):
    r = gate(git_repo, tmp_path, ocr_bin=str(tmp_path / "nope" / "ocr"))
    assert r.returncode == 2, r.stderr
    assert "not found" in r.stderr.lower()
    # The whole point: a missing gate never reads as a clean gate.
    assert r.returncode != 0


def test_non_executable_binary_is_2(git_repo, tmp_path):
    dead = tmp_path / "dead-ocr"
    dead.write_text("#!/bin/sh\nexit 0\n")
    r = gate(git_repo, tmp_path, ocr_bin=str(dead))
    assert r.returncode == 2, r.stderr
    assert "not an executable" in r.stderr.lower()


def test_clean_tree_with_binary_passes_vacuously(git_repo, tmp_path):
    stub = make_stub(
        tmp_path,
        payload={
            "schema_version": "1",
            "mode": "workspace",
            "total_files": 0,
            "reviewable_count": 0,
            "excluded_files": [],
            "reviewable_files": [],
            "total_insertions": 0,
            "total_deletions": 0,
        },
    )
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 0, r.stderr
    assert "nothing to review" in r.stdout


# --- happy path --------------------------------------------------------------

REVIEWABLE = {
    "schema_version": "1",
    "mode": "commit",
    "total_files": 3,
    "reviewable_count": 2,
    "total_insertions": 100,
    "total_deletions": 4,
    "reviewable_files": [
        {"path": "app/transparency_routes.py", "status": "modified", "insertions": 80, "deletions": 4},
        {"path": "app/models.py", "status": "modified", "insertions": 20, "deletions": 0},
    ],
    "excluded_files": [
        {
            "path": "tests/test_provenance.py",
            "status": "added",
            "insertions": 453,
            "deletions": 0,
            "exclude_reason": "default_path",
        },
        {
            "path": "README.md",
            "status": "modified",
            "insertions": 2,
            "deletions": 2,
            "exclude_reason": "unsupported_ext",
        },
    ],
}


def test_reviewable_diff_passes_and_emits_next_command(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload=REVIEWABLE)
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 0, r.stderr
    assert "reviewable=2" in r.stdout
    assert "app/transparency_routes.py" in r.stdout
    assert "delegate rule" in r.stdout  # tells the agent what to do next
    assert "app/models.py" in r.stdout


def test_excluded_test_files_are_announced_not_swallowed(git_repo, tmp_path):
    """The 70%-of-the-diff-dropped-silently shape (measured on PR #474)."""
    stub = make_stub(tmp_path, payload=REVIEWABLE)
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 0
    assert "EXCLUDED tests/test_provenance.py" in r.stdout
    assert "reason=default_path" in r.stdout
    # It must hand back a command that actually covers the dropped file.
    assert "delegate rule --from main" in r.stdout
    assert "tests/test_provenance.py" in r.stdout.split("EXCLUDED")[-1]
    # and quantify what was dropped
    assert "files ocr will NOT review" in r.stdout


def test_paths_with_spaces_survive(git_repo, tmp_path):
    payload = dict(
        REVIEWABLE,
        reviewable_files=[
            {"path": "app/a file with spaces.py", "status": "added", "insertions": 5, "deletions": 0}
        ],
    )
    stub = make_stub(tmp_path, payload=payload)
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 0, r.stderr
    assert "app/a file with spaces.py" in r.stdout
    # quoted, so the printed command is copy-pasteable and not word-split
    assert "'app/a file with spaces.py'" in r.stdout


def test_evidence_json_is_written(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload=REVIEWABLE)
    out = tmp_path / "evidence.json"
    r = gate(git_repo, tmp_path, stub=stub, extra=["--json", str(out)])
    assert r.returncode == 0, r.stderr
    assert out.exists()
    assert json.loads(out.read_text())["reviewable_count"] == 2


# --- the manufactured-assurance case -----------------------------------------

ZEROED = {
    "schema_version": "1",
    "mode": "workspace",
    "total_files": 3,
    "reviewable_count": 0,
    "total_insertions": 40,
    "total_deletions": 1,
    "reviewable_files": [],
    "excluded_files": [
        {
            "path": "README.md",
            "status": "modified",
            "insertions": 30,
            "deletions": 1,
            "exclude_reason": "unsupported_ext",
        },
        {
            "path": "docs/plan.md",
            "status": "added",
            "insertions": 10,
            "deletions": 0,
            "exclude_reason": "unsupported_ext",
        },
    ],
}


def test_docs_only_diff_is_not_a_silent_pass(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload=ZEROED)
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 4, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "REVIEW DID NOT HAPPEN" in r.stderr


def test_zero_reviewable_can_be_authorised_explicitly(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload=ZEROED)
    r = gate(git_repo, tmp_path, stub=stub, ack="docs-only change")
    assert r.returncode == 0, r.stderr
    assert "docs-only change" in r.stderr  # the reason must be visible
    assert "PR body" in r.stderr  # and pointed at where it belongs


# --- failure paths: rule 3 ----------------------------------------------------


def test_ocr_failure_is_reported_not_skipped(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload={}, exit_code=1)
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 3, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "rule 3" in r.stderr
    # the actual exit code of ocr must reach the operator, not be laundered
    assert "exit 1" in r.stderr


def test_unparseable_output_fails_closed(git_repo, tmp_path):
    stub = make_stub(tmp_path, raw="this is not json {{{")
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 3, r.stderr
    assert "parse" in r.stderr.lower()


def test_json_that_is_not_an_object_fails_closed(git_repo, tmp_path):
    stub = make_stub(tmp_path, raw="[1,2,3]")
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 3, r.stderr


def test_missing_counters_fail_closed(git_repo, tmp_path):
    """No reviewable_count => we cannot confirm a review happened => not a pass."""
    stub = make_stub(tmp_path, payload={"schema_version": "1", "mode": "workspace"})
    r = gate(git_repo, tmp_path, stub=stub)
    assert r.returncode == 3, r.stderr
    assert "reviewable_count" in r.stderr


# --- argument plumbing --------------------------------------------------------


def test_range_flags_reach_ocr(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload=REVIEWABLE)
    r = gate(git_repo, tmp_path, stub=stub, extra=["--from", "main", "--to", "feature", "-c", "abc1234"])
    assert r.returncode == 0, r.stderr
    argv = (tmp_path / "argv.txt").read_text()
    for token in ("--from main", "--to feature", "-c abc1234"):
        assert token in argv, f"{token!r} was dropped before reaching ocr: {argv}"


def test_unknown_argument_is_2(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload=REVIEWABLE)
    r = gate(git_repo, tmp_path, stub=stub, extra=["--wat"])
    assert r.returncode == 2, r.stderr
    assert "unknown argument" in r.stderr


def test_missing_value_for_flag_is_2(git_repo, tmp_path):
    stub = make_stub(tmp_path, payload=REVIEWABLE)
    r = gate(git_repo, tmp_path, stub=stub, extra=["--from"])
    assert r.returncode == 2, r.stderr


def test_non_git_repo_is_2(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    stub = make_stub(tmp_path, payload=REVIEWABLE)
    r = gate(plain, tmp_path, stub=stub)
    assert r.returncode == 2, r.stderr


def test_gate_script_is_executable_and_documented():
    """A gate nobody can run is how this card got written in the first place."""
    assert GATE.exists()
    assert os.access(GATE, os.X_OK), "scripts/ocr-gate.sh must be chmod +x"
    text = GATE.read_text()
    for token in ("delegate preview", "reviewable_count", "default_path"):
        assert token in text
    # the exit contract in the header must match what the tests assert
    for code in ("0", "2", "3", "4"):
        assert code in text
