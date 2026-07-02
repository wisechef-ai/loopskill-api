"""Tests for scripts/bulk_close_drift_probe_noise.py.

Uses a stub `gh` binary via PATH override to avoid real GitHub API calls.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "bulk_close_drift_probe_noise.py"


def make_stub_gh(tmp_path: Path, issues: list[dict], close_rc: int = 0) -> Path:
    """Write a minimal stub gh CLI script and return the bin directory."""
    issues_json = json.dumps(issues)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    gh_stub = stub_bin / "gh"

    # The stub records close calls to a log file and returns close_rc for them.
    gh_stub.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "issue" ] && [ "$2" = "list" ]; then
                echo '{issues_json}'
                exit 0
            fi
            if [ "$1" = "issue" ] && [ "$2" = "close" ]; then
                echo "$@" >> "{tmp_path}/close_calls.txt"
                exit {close_rc}
            fi
            echo "stub: unhandled $*" >&2
            exit 1
            """
        )
    )
    gh_stub.chmod(gh_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_bin


def run_script(stub_bin: Path, extra_args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the bulk-close script with a patched PATH."""
    env = os.environ.copy()
    env["PATH"] = str(stub_bin) + ":" + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + extra_args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

ISSUES_NO_COMMENTS = [
    {"number": 100, "title": "[recipe:bug] larry — abc123", "createdAt": "2026-05-22T10:00:00Z", "comments": []},
    {"number": 101, "title": "[recipe:bug] pr-draft — def456", "createdAt": "2026-05-22T11:00:00Z", "comments": []},
    {"number": 102, "title": "[recipe:bug] graphify — ghi789", "createdAt": "2026-05-22T12:00:00Z", "comments": []},
]

ISSUES_WITH_COMMENT = [
    {"number": 200, "title": "[recipe:bug] larry — abc123", "createdAt": "2026-05-22T10:00:00Z", "comments": []},
    {
        "number": 201,
        "title": "[recipe:bug] pr-draft — has-human",
        "createdAt": "2026-05-22T11:00:00Z",
        "comments": [{"author": {"login": "adam"}, "body": "I see this too!"}],
    },
]


# ---------------------------------------------------------------------------
# Test 1: --dry-run prints IDs and does NOT call `gh issue close`
# ---------------------------------------------------------------------------

def test_dry_run_no_close_calls(tmp_path: Path) -> None:
    stub_bin = make_stub_gh(tmp_path, ISSUES_NO_COMMENTS)
    result = run_script(stub_bin, ["--dry-run"], tmp_path)
    assert result.returncode == 0, result.stderr

    # Must print DRY lines for each issue
    for issue in ISSUES_NO_COMMENTS:
        assert f"DRY   #{issue['number']}" in result.stdout

    # Must NOT have called close
    close_log = tmp_path / "close_calls.txt"
    assert not close_log.exists(), "dry-run must not call gh issue close"

    # Summary says DRY-RUN
    assert "DRY-RUN summary" in result.stdout
    assert "would close:              3" in result.stdout


# ---------------------------------------------------------------------------
# Test 2: issues with non-empty comments are skipped
# ---------------------------------------------------------------------------

def test_skips_issues_with_comments(tmp_path: Path) -> None:
    stub_bin = make_stub_gh(tmp_path, ISSUES_WITH_COMMENT)
    result = run_script(stub_bin, ["--confirm"], tmp_path)
    assert result.returncode == 0, result.stderr

    # #200 should be closed, #201 should be skipped
    close_log = tmp_path / "close_calls.txt"
    assert close_log.exists()
    calls = close_log.read_text()
    assert "200" in calls
    assert "201" not in calls

    assert "SKIP  #201" in result.stdout
    assert "skipped (has comments):   1" in result.stdout


# ---------------------------------------------------------------------------
# Test 3: AUTHOR constant is hardcoded to app/github-actions
# ---------------------------------------------------------------------------

def test_author_is_github_actions() -> None:
    """Grep the script source to verify the author filter is hardcoded."""
    source = SCRIPT.read_text()
    assert "app/github-actions" in source, (
        "AUTHOR must be hardcoded to 'app/github-actions' in the script"
    )


# ---------------------------------------------------------------------------
# Test 4: --max caps the number of closures
# ---------------------------------------------------------------------------

def test_max_caps_closures(tmp_path: Path) -> None:
    stub_bin = make_stub_gh(tmp_path, ISSUES_NO_COMMENTS)
    # max=1 should close only 1 of 3
    result = run_script(stub_bin, ["--confirm", "--max", "1"], tmp_path)
    assert result.returncode == 0, result.stderr

    close_log = tmp_path / "close_calls.txt"
    assert close_log.exists()
    # Count lines that start with "issue close" (each invocation starts a new entry)
    calls_text = close_log.read_text()
    close_invocations = [l for l in calls_text.splitlines() if l.startswith("issue close")]
    assert len(close_invocations) == 1, (
        f"Expected 1 close invocation, got {len(close_invocations)}: {close_invocations}"
    )

    assert "Reached --max 1" in result.stdout
    assert "closed:                   1" in result.stdout


# ---------------------------------------------------------------------------
# Test 5: --dry-run requires no --confirm (mutually exclusive)
# ---------------------------------------------------------------------------

def test_mutual_exclusion(tmp_path: Path) -> None:
    stub_bin = make_stub_gh(tmp_path, ISSUES_NO_COMMENTS)
    result = run_script(stub_bin, ["--dry-run", "--confirm"], tmp_path)
    assert result.returncode != 0
    assert "not allowed with" in result.stderr or "error" in result.stderr.lower()
