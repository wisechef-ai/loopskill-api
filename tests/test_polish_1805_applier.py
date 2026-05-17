"""polish_1805 item 3 — applier must lint & idempotently update descriptions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "polish_1805_apply_descriptions.py"


def test_lint_rejects_long_description(tmp_path):
    """A rewrite >200 chars must fail lint."""
    data = {
        "rewrites": [
            {"slug": "demo", "old": "x", "new": "y" * 250, "char_count": 250},
        ],
        "stats": {"total": 1, "rewritten": 1, "kept_original": 0},
    }
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(data))
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(f)],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert r.returncode == 3, f"expected lint exit, got {r.returncode}: {r.stdout}\n{r.stderr}"
    assert "LINT FAILED" in r.stdout


def test_lint_rejects_banned_phrase(tmp_path):
    """A rewrite with banned marketing language must fail lint."""
    data = {
        "rewrites": [
            {"slug": "demo", "old": "x", "new": "An amazing tool that ships.", "char_count": 27},
        ],
    }
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(data))
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(f)],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert r.returncode == 3
    assert "amazing" in r.stdout.lower() or "banned phrase" in r.stdout


def test_real_batch_passes_lint(tmp_path):
    """The actual Haiku-generated batch shipped in this PR must pass lint."""
    real = Path(__file__).resolve().parent.parent / "scripts" / "polish_1805_descriptions.json"
    if not real.exists():
        pytest.skip("real batch not present in this checkout")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(real), "--lint-only"],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert r.returncode == 0, (
        f"Shipped batch failed lint! exit={r.returncode}\n{r.stdout}\n{r.stderr}"
    )
    assert "Lint pass" in r.stdout, r.stdout
    assert "LINT FAILED" not in r.stdout
