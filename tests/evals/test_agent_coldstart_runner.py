"""Tests for evals/agent_coldstart/run.py + report.py.

Uses the `fake` harness exclusively (no network, no real harness binaries)
so this suite runs safely in CI. Never edits tasks.yaml (read-only fixture).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "evals" / "agent_coldstart"
sys.path.insert(0, str(EVAL_DIR))

import report as report_mod  # noqa: E402
import run as run_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_fake_env(monkeypatch):
    """Ensure fake-harness env knobs never leak between tests."""
    for var in (
        "FAKE_HARNESS_CMD",
        "FAKE_HARNESS_TOOL_CALLS",
        "FAKE_HARNESS_TOKENS_IN",
        "FAKE_HARNESS_TOKENS_OUT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_tasks_yaml_parses_and_every_task_has_required_fields():
    suite = run_mod.load_tasks()
    assert suite["tasks"], "expected at least one task"
    for task in suite["tasks"]:
        assert task.get("id"), f"task missing id: {task}"
        assert task.get("prompt"), f"task {task.get('id')} missing prompt"
        assert task.get("success_check"), f"task {task.get('id')} missing success_check"
        assert task.get("max_minutes"), f"task {task.get('id')} missing max_minutes"
        assert isinstance(task["max_minutes"], int)


def test_fresh_home_has_no_loopskill_secrets(tmp_path, monkeypatch):
    """Isolation contract: a brand-new $HOME must start with zero hits."""
    home = tmp_path / "fresh_home"
    home.mkdir()
    # Should not raise on a genuinely clean tree.
    run_mod.assert_no_secret_leak(home)

    # And it must actually detect a leak if one is present.
    leaky = home / "leftover.txt"
    leaky.write_text("api_key=rec_live_abc123 for loopskill")
    with pytest.raises(run_mod.ColdstartError):
        run_mod.assert_no_secret_leak(home)


def test_run_task_fake_harness_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_CMD", 'echo hi > "$HOME/.loopskill_marker"; true')
    monkeypatch.setenv("FAKE_HARNESS_TOOL_CALLS", "3")
    results_dir = tmp_path / "results"

    # Patch get_task with a synthetic always-pass task so this test does not
    # depend on live network calls baked into real tasks.yaml checks.
    fake_task = {
        "id": "fake-pass-task",
        "prompt": "do nothing",
        "success_check": "exit 0",
        "max_minutes": 1,
    }
    monkeypatch.setattr(run_mod, "get_task", lambda task_id: fake_task)

    result = run_mod.run_task("fake", "fake-pass-task", results_dir, run_id="aaaa1111")
    assert result.outcome == "pass"
    assert result.check_exit == 0
    assert result.tool_calls == 3

    out_files = list(results_dir.glob("*.json"))
    assert len(out_files) == 1
    payload = json.loads(out_files[0].read_text())
    assert payload["outcome"] == "pass"


def test_run_task_fake_harness_check_fail(tmp_path, monkeypatch):
    fake_task = {
        "id": "fake-fail-task",
        "prompt": "do nothing",
        "success_check": "exit 7",
        "max_minutes": 1,
    }
    monkeypatch.setattr(run_mod, "get_task", lambda task_id: fake_task)
    result = run_mod.run_task("fake", "fake-fail-task", tmp_path / "results", run_id="bbbb2222")
    assert result.outcome == "fail"
    assert result.check_exit == 7


def test_run_task_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_CMD", "sleep 5")
    fake_task = {
        "id": "fake-timeout-task",
        "prompt": "do nothing",
        "success_check": "exit 0",
        # max_minutes must be > 0 for our harness invocation math (seconds
        # = minutes * 60); use a fractional minute via env override instead.
        "max_minutes": 1,
    }
    monkeypatch.setattr(run_mod, "get_task", lambda task_id: fake_task)
    # Shrink the effective per-run timeout without touching tasks.yaml's
    # contract: patch max_minutes conversion by monkeypatching the harness
    # call path's timeout unit indirectly via a tiny sleep vs small budget.
    # Simplest: temporarily override the task's max_minutes to a very small
    # fraction is not expressible in int minutes, so instead we shrink the
    # sleep-vs-budget relationship by patching subprocess timeout directly.
    import subprocess as _subprocess

    real_run = _subprocess.run

    def _short_timeout_run(cmd, **kwargs):
        if "timeout" in kwargs:
            kwargs["timeout"] = 0.2
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(run_mod.subprocess, "run", _short_timeout_run)

    result = run_mod.run_task("fake", "fake-timeout-task", tmp_path / "results", run_id="cccc3333")
    assert result.outcome == "timeout"


def test_missing_harness_binary_is_error_not_fail(tmp_path, monkeypatch):
    fake_task = {
        "id": "fake-missing-binary-task",
        "prompt": "install something",
        "success_check": "exit 0",
        "max_minutes": 1,
    }
    monkeypatch.setattr(run_mod, "get_task", lambda task_id: fake_task)
    monkeypatch.setattr(run_mod.shutil, "which", lambda name: None)

    result = run_mod.run_task("claude", "fake-missing-binary-task", tmp_path / "results", run_id="dddd4444")
    assert result.outcome == "error"
    assert "not found on PATH" in result.check_tail


def test_report_math_excludes_failed_tasks_from_cost_to_value(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    def write(name, outcome, tool_calls, wall_seconds):
        payload = {
            "run_id": name,
            "started_at": "2026-01-01T00:00:00Z",
            "host": "test-host",
            "harness": "fake",
            "model": None,
            "task_id": f"task-{name}",
            "outcome": outcome,
            "tool_calls": tool_calls,
            "tokens_in": None,
            "tokens_out": None,
            "wall_seconds": wall_seconds,
            "check_exit": 0 if outcome == "pass" else 1,
            "check_tail": "",
            "transcript_path": "",
        }
        (results_dir / f"{name}.json").write_text(json.dumps(payload))

    # Two cheap passes, one very expensive failure (60 tool calls burned).
    write("r1", "pass", 4, 90.0)
    write("r2", "pass", 6, 120.0)
    write("r3", "fail", 60, 700.0)

    results = report_mod.load_results(results_dir)
    summary = report_mod.summarize(results)
    fake_summary = summary["harnesses"]["fake"]

    # cost-to-value must only reflect the two passed runs (4, 6 tool_calls)
    assert fake_summary["cost_to_value_passed_only"]["median_tool_calls"] == 5
    # the expensive failure must NOT drag the median up
    assert fake_summary["cost_to_value_passed_only"]["median_tool_calls"] != 60
    assert fake_summary["failed_task_effort"]["median_tool_calls_burned"] == 60
    assert fake_summary["pass_rate"] == "2/3"


def test_seed_files_content_written_verbatim(tmp_path):
    task = {
        "id": "seed-task",
        "preconditions": [
            {
                "seed_files": [
                    {"path": "~/my-skill/SKILL.md", "content": "hello {{run_id}}"},
                ]
            }
        ],
    }
    home = tmp_path / "home"
    home.mkdir()
    run_mod.seed_preconditions(task, home, "deadbeef")
    seeded = home / "my-skill" / "SKILL.md"
    assert seeded.read_text() == "hello deadbeef"


def test_render_substitutes_run_id():
    assert run_mod.render("skill-{{run_id}}", "cafe1234") == "skill-cafe1234"


def test_get_task_unknown_raises():
    with pytest.raises(KeyError):
        run_mod.get_task("this-task-does-not-exist")


def test_all_real_task_ids_present():
    """Sanity: the task ids this brief names for real runs actually exist."""
    suite = run_mod.load_tasks()
    ids = {t["id"] for t in suite["tasks"]}
    for expected in ("install-named-skill", "read-llms-txt-qa"):
        assert expected in ids
