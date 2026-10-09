"""Runner-bug regression (coldstart_0609, run 20261009-77b2aec1): `codex exec
--json` reports HARNESS failures — account usage limit, auth, quota — as a
top-level `{"type": "error", ...}` event followed by `{"type":
"turn.failed", ...}` with zero tool items. The runner scored that stillborn
leg as `fail` ("codex 0/1"), charging an account-quota artifact against the
product and pushing the task over the 3-strike BLOCKED line.

Per RUBRIC.md a harness failure is outcome `error` (excluded from the pass
rate) — the same contract already implemented for claude (`is_error`, #321)
and hermes ("No usable credentials", d46b67c). This file closes the third
instance of that bug class for codex.

The test fakes the `codex` binary on PATH so nothing touches a real account.
The stillborn fixture is the real transcript of run 20261009-77b2aec1
(thread.started / turn.started / error / turn.failed, 3.77s, 0 tool calls:
"You've hit your usage limit ... try again at 4:29 AM.").
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


# --- fixtures: real transcript shapes from run 20261009-77b2aec1 -------------

STILLBORN_QUOTA_EVENTS = [
    {"type": "thread.started", "thread_id": "01a11e12-01b8-7671-8062-4898c6c11bde"},
    {"type": "turn.started"},
    {
        "type": "error",
        "message": (
            "You’ve hit your usage limit. Upgrade to Pro "
            "(https://chatgpt.com/explore/pro), visit "
            "https://chatgpt.com/codex/settings/usage to purchase more credits "
            "or try again at 4:29 AM."
        ),
    },
    {
        "type": "turn.failed",
        "error": {
            "message": (
                "You’ve hit your usage limit. Upgrade to Pro "
                "(https://chatgpt.com/explore/pro), visit "
                "https://chatgpt.com/codex/settings/usage to purchase more credits "
                "or try again at 4:29 AM."
            )
        },
    },
]

# A healthy leg: turn starts, one command item completes, turn completes.
HEALTHY_EVENTS = [
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "curl -s https://app.loopskill.io/"},
    },
    {"type": "turn.completed"},
]


def _fake_codex(bin_dir: Path, events: list[dict], stderr: str = "") -> None:
    """Write a fake `codex` binary emitting exactly these JSONL events."""
    lines = "".join(json.dumps(e) + "\n" for e in events)
    script = bin_dir / "codex"
    script.write_text(
        "#!/bin/sh\n"
        "cat <<'JSONL'\n"
        + lines
        + "JSONL\n"
        + ("cat <<'STDERR' >&2\n" + stderr + "\nSTDERR\n" if stderr else "")
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_codex_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # never copy real OAuth creds into the fake home: Path.home() reads $HOME
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    (tmp_path / "nohome").mkdir()
    return bin_dir


def test_codex_turn_failed_becomes_harness_error(fake_codex_path, tmp_path):
    run = _load_run()
    _fake_codex(fake_codex_path, STILLBORN_QUOTA_EVENTS)
    res = run.run_codex_harness("do the task", tmp_path / "home", max_minutes=1)
    assert res.error is not None, (
        "turn.failed with zero tool items must surface as a harness error, not a silent fail"
    )
    assert "usage limit" in res.error
    assert res.timed_out is False
    assert res.tool_calls == 0


def test_codex_error_event_without_turn_failed_also_errors(fake_codex_path, tmp_path):
    run = _load_run()
    events = [e for e in STILLBORN_QUOTA_EVENTS if e["type"] != "turn.failed"]
    _fake_codex(fake_codex_path, events)
    res = run.run_codex_harness("do the task", tmp_path / "home", max_minutes=1)
    assert res.error is not None, "a top-level error event must surface as a harness error"
    assert "usage limit" in res.error


def test_codex_healthy_run_has_no_harness_error(fake_codex_path, tmp_path):
    run = _load_run()
    _fake_codex(fake_codex_path, HEALTHY_EVENTS)
    res = run.run_codex_harness("do the task", tmp_path / "home", max_minutes=1)
    assert res.error is None
    assert res.tool_calls == 1


def test_codex_agent_turn_failure_after_real_work_stays_fail(fake_codex_path, tmp_path):
    """A turn.failed AFTER real tool work is agent behavior, not stillbirth.

    Negative control against over-matching: if the agent did real work and the
    turn then failed (e.g. it errored itself), that leg must keep scoring
    `fail`, not launder into `error`.
    """
    run = _load_run()
    events = HEALTHY_EVENTS[:-1] + [
        STILLBORN_QUOTA_EVENTS[3],  # turn.failed after the completed command item
    ]
    _fake_codex(fake_codex_path, events)
    res = run.run_codex_harness("do the task", tmp_path / "home", max_minutes=1)
    assert res.error is None, "turn.failed after real tool work must NOT be reclassified as harness error"
    assert res.tool_calls == 1
