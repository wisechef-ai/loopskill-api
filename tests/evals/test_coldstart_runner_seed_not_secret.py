"""Runner-bug regression: a seeded task file that legitimately MENTIONS the
product name in prose must not trip the isolation scan (coldstart_0609 first
live run, publish-throwaway-skill -> error "isolation violated" because the
blind-authored seed SKILL.md says "LoopSkill").

Contract being pinned: the isolation scan exists to prove a COLD start (no key,
no MCP config) — it must match credential-shaped markers (rec_live…/rec_agent…),
not the product name in documentation prose. The exact fix direction (narrow
the pattern, or scan only credential-bearing file types) is the fix lane's to
make; this test locks the behaviour either way by asserting the REAL seed file
from tasks.yaml passes the scan.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]  # tests/evals/ -> repo root
sys.path.insert(0, str(REPO / "evals" / "agent_coldstart"))
import run  # noqa: E402


def _seed_file_of(task_id: str) -> tuple[str, str]:
    tasks = yaml.safe_load((REPO / "evals" / "agent_coldstart" / "tasks.yaml").read_text())["tasks"]
    t = next(x for x in tasks if x["id"] == task_id)
    for pre in t.get("preconditions") or []:
        if isinstance(pre, dict) and "seed_files" in pre:
            seed = pre["seed_files"][0]
            return seed["path"], seed["content"].replace("{{run_id}}", "deadbeef")
    raise AssertionError(f"no seed_files on {task_id}")


def test_seeded_prose_mentioning_product_name_is_not_a_secret_leak():
    """RED on current main: the publish task's own seed SKILL.md trips the scan."""
    path, content = _seed_file_of("publish-throwaway-skill")
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        (home / path.lstrip("~/")).parent.mkdir(parents=True, exist_ok=True)
        (home / path.lstrip("~/")).write_text(content)
        run.assert_no_secret_leak(home)  # must NOT raise


def test_real_credential_markers_still_trip_the_scan():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        (home / ".env").write_text("LOOPSKILL_API_KEY=rec_live_abc123\n")
        try:
            run.assert_no_secret_leak(home)
        except run.ColdstartError:
            pass
        else:
            raise AssertionError("a rec_live credential must still be caught")
        (home / ".config" / "hermes").mkdir(parents=True)
        (home / ".config" / "hermes" / "config.yaml").write_text(
            "mcp_servers:\n  loopskill:\n    url: https://app.loopskill.io/api/mcp/http/\n"
        )
        try:
            run.assert_no_secret_leak(home)
        except run.ColdstartError:
            pass
        else:
            raise AssertionError("a pre-wired loopskill MCP config must still be caught")
