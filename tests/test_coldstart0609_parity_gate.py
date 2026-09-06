"""Frozen-surface parity gate (coldstart_0609).

``scripts/parity_dump.py`` snapshots every machine-facing surface (OpenAPI,
routes, MCP tool schemas, alembic heads, public-path allow-list, CLI help) into
``evals/parity/baseline.json``. This test makes an UNDECLARED change to any of
them a red CI run: agents in the wild pin to these shapes, so a schema drift
that every unit test tolerates is still a regression.

To change a surface on purpose, run ``python scripts/parity_dump.py update`` in
the same PR and describe the change — the baseline diff in the PR IS the
declaration.

The pure logic (hashing, ordering, diff, --allow) is covered by the script's own
``--self-test``; this module proves (a) the self-test passes, (b) the committed
baseline matches the tree under test, (c) a mutated surface is detected.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "parity_dump.py"
BASELINE = REPO / "evals" / "parity" / "baseline.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("parity_dump", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parity_script_self_test_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_committed_baseline_matches_tree() -> None:
    """The contract: every surface hash in evals/parity/baseline.json equals the tree's."""
    assert BASELINE.exists(), "no parity baseline — run `python scripts/parity_dump.py update`"
    mod = _load_module()
    current = mod.build_dump(mod._collect())
    baseline = json.loads(BASELINE.read_text())
    drifted, report = mod.compare(baseline, current, allow=set())
    assert drifted == [], (
        "undeclared surface drift in "
        + ", ".join(drifted)
        + " — if intentional, run `python scripts/parity_dump.py update` in this PR\n"
        + report
    )


def test_mutated_surface_is_detected() -> None:
    """RED-proof: the gate must catch a single extra MCP tool / route, not just wholesale rewrites."""
    mod = _load_module()
    surfaces = mod._collect()
    baseline = mod.build_dump(surfaces)
    surfaces["mcp_tools"].append({"name": "zzz_injected", "description": "", "inputSchema": {}})
    surfaces["routes"].append("GET /api/injected")
    drifted, report = mod.compare(baseline, mod.build_dump(surfaces), allow=set())
    assert drifted == ["routes", "mcp_tools"], drifted
    assert "zzz_injected" in report and "/api/injected" in report
    # a declared change is not a drift
    drifted, _ = mod.compare(baseline, mod.build_dump(surfaces), allow={"routes", "mcp_tools"})
    assert drifted == []
