"""Runner-bug regression (coldstart_0609 first live run): `claude -p` reports
HARNESS failures — account session limit, auth, quota — as `is_error: true`
with the message in `result`. The runner scored all 10 of those rows as
`fail`, i.e. charged an account artifact against the product. Per RUBRIC a
harness failure is outcome `error` (excluded from the pass rate).

The test fakes the `claude` binary on PATH so nothing touches a real account.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "evals" / "agent_coldstart"))

import run as run_mod  # noqa: E402


def _load_run():
    return run_mod


def _fake_claude(bin_dir: Path, payload: dict) -> None:
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\ncat <<'JSON'\n" + json.dumps(payload) + "\nJSON\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_claude_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # never copy real OAuth creds into the fake home: Path.home() reads $HOME
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    (tmp_path / "nohome").mkdir()
    return bin_dir


def test_claude_is_error_payload_becomes_harness_error(fake_claude_path, tmp_path):
    run = _load_run()
    _fake_claude(
        fake_claude_path,
        {
            "is_error": True,
            "result": "You've hit your session limit · resets 7pm (Europe/Warsaw)",
            "num_turns": 1,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    )
    res = run.run_claude_harness("do the task", tmp_path / "home", max_minutes=1)
    assert res.error is not None, "is_error payload must surface as a harness error, not a silent fail"
    assert "session limit" in res.error
    assert res.timed_out is False


def test_claude_success_payload_has_no_harness_error(fake_claude_path, tmp_path):
    run = _load_run()
    _fake_claude(
        fake_claude_path,
        {
            "is_error": False,
            "result": "done",
            "num_turns": 7,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    res = run.run_claude_harness("do the task", tmp_path / "home", max_minutes=1)
    assert res.error is None
    assert res.tool_calls == 7
