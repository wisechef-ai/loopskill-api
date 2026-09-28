"""coldstart_0928 — success_check must be rendered like every other task field.

Transcript evidence: every recorded verdict for ``publish-throwaway-skill`` —
the ONLY task whose ``success_check`` contains ``{{run_id}}`` — died with
``curl: (3) nested brace in URL ... coldstart-bench-{{run_id}}`` (claude runs
20260913-021ad7f7, 20260914-2b4cf18d, 20260928-4a02d868; codex
20260915-5174e457). The runner rendered ``prompt`` and ``seed_files`` through
``render()`` but passed ``task["success_check"]`` to bash UNRENDERED, so the
check crashed on the literal template marker before opening a socket. The task
was unpassable-by-construction on every harness, and the ping-pong breaker
BLOCKED all three harness:task pairs after "3 attempts, no improvement" that
could never improve — an instrument artifact, not a product verdict.

These tests pin:
  1. ``run_task()`` executes the check with ``{{run_id}}`` substituted;
  2. any template marker still left in a check after rendering fails LOUD as
     ``outcome="error"`` (RUBRIC.md: a broken instrument is an error, never a
     product fail) instead of silently running garbage bash;
  3. every task in tasks.yaml renders clean (no unknown ``{{var}}`` markers),
     so future task authors cannot reintroduce this class silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "evals" / "agent_coldstart"
sys.path.insert(0, str(EVAL_DIR))

import run as run_mod  # noqa: E402


def _fake_home(tmp_path):
    home = tmp_path / "cold-home"
    home.mkdir()
    return home


def test_run_task_renders_run_id_in_success_check(tmp_path, monkeypatch):
    """The check must see the RENDERED run_id, not the literal marker."""
    home = _fake_home(tmp_path)
    monkeypatch.setattr(run_mod, "make_cold_home", lambda: home)
    fake_task = {
        "id": "render-probe-task",
        "prompt": "do nothing",
        # Writes the marker's post-render value into $HOME so the test can
        # assert what the check ACTUALLY executed with.
        "success_check": 'printf "%s" "{{run_id}}" > "$HOME/rendered.txt"',
        "max_minutes": 1,
    }
    monkeypatch.setattr(run_mod, "get_task", lambda task_id: fake_task)

    result = run_mod.run_task("fake", "render-probe-task", tmp_path / "results", run_id="deadbeef")
    assert result.outcome == "pass", result.check_tail
    assert (home / "rendered.txt").read_text() == "deadbeef"


def test_unrendered_template_marker_in_check_is_error_not_fail(tmp_path, monkeypatch):
    """A check that still contains a {{var}} marker after rendering must be a
    loud runner error (RUBRIC.md), never a silent product fail."""
    home = _fake_home(tmp_path)
    monkeypatch.setattr(run_mod, "make_cold_home", lambda: home)
    fake_task = {
        "id": "stale-marker-task",
        "prompt": "do nothing",
        # {{run_uuid}} is not a known template var -> must survive render()
        # -> the runner must refuse to execute it as bash.
        "success_check": 'echo "{{run_uuid}}"',
        "max_minutes": 1,
    }
    monkeypatch.setattr(run_mod, "get_task", lambda task_id: fake_task)

    result = run_mod.run_task("fake", "stale-marker-task", tmp_path / "results", run_id="deadbeef")
    assert result.outcome == "error"
    assert "unrendered" in result.check_tail
    assert "{{run_uuid}}" in result.check_tail


def test_every_tasks_yaml_check_renders_clean():
    """Invariant: after substituting every known template var, no task's
    success_check may still contain a {{word}} marker."""
    for task in run_mod.load_tasks()["tasks"]:
        rendered = run_mod.render(task["success_check"], "probe")
        assert not run_mod.UNRENDERED_TEMPLATE_RE.search(rendered), (
            f"task {task['id']} success_check keeps a template marker after render: {rendered!r}"
        )
