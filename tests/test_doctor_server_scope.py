"""Tests for loopskill_doctor — issue #112.

Verifies the three error codes:
- ``install_dir_required`` for empty input
- ``not_server_inspectable`` for paths shaped like an agent's host
- ``install_dir_not_found`` for server-local paths that don't exist
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock

import pytest

from app.mcp.tools.doctor import _looks_like_remote_path, loopskill_doctor


@pytest.fixture
def db():
    return MagicMock()


def test_empty_input_returns_install_dir_required(db):
    out = loopskill_doctor(db, "")
    assert out["ok"] is False
    assert out["error"] == "install_dir_required"
    assert "agent" in out["hint"].lower()


@pytest.mark.parametrize(
    "remote_path",
    [
        "/home/adam/.hermes/skills/foo",
        "/home/wisevision/.hermes/skills/foo",
        "/Users/wisevision/.hermes/skills/foo",
        "/Users/alice/.claude/skills/foo",
        "~/skills/foo",
        "C:\\Users\\bob\\skills\\foo",
        "C:/Users/bob/skills/foo",
        "D:\\agent\\skills\\foo",
    ],
)
def test_remote_shaped_path_returns_not_server_inspectable(db, remote_path):
    out = loopskill_doctor(db, remote_path)
    assert out["ok"] is False, f"unexpected ok=True for {remote_path}"
    assert out["error"] == "not_server_inspectable", (
        f"expected not_server_inspectable for {remote_path}, got {out['error']}"
    )
    # Hint must explicitly call out the server-side scope so agents stop
    # interpreting this as "path doesn't exist."
    assert "server-side" in out["hint"] or "server" in out["hint"]
    assert out["install_dir"] == remote_path


def test_server_local_missing_path_returns_install_dir_not_found(db):
    # A fixed server-shaped root (the server lives under /srv/ or /var/), NOT
    # pytest's tmp_path: tmp_path follows $TMPDIR, and when that resolves
    # under /home/<user>/ (agent sandboxes, CI runners with a home-scoped
    # tmp) the path is remote-shaped by design and the code correctly answers
    # not_server_inspectable — the test, not the code, was host-dependent.
    missing = f"/srv/loopskill/skills/does-not-exist-{uuid.uuid4().hex}"
    assert not os.path.exists(missing)
    assert not _looks_like_remote_path(missing)
    out = loopskill_doctor(db, missing)
    assert out["ok"] is False
    assert out["error"] == "install_dir_not_found"
    assert out["install_dir"] == missing


def test_valid_install_dir_no_violations(db, tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: testskill\ndescription: portable\n---\n# Test skill\n"
    )
    meta = tmp_path / "_meta.json"
    meta.write_text(json.dumps({"slug": "testskill", "version": "1.0.0"}))
    out = loopskill_doctor(db, str(tmp_path))
    assert out["ok"] is True
    assert out["skill_md_present"] is True
    assert out["meta_present"] is True
    assert out["meta_valid"] is True
    assert out["hardcoded_paths"] == {}


def test_install_dir_with_hardcoded_paths_flags_them(db, tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: x\n---\nbody")
    (tmp_path / "_meta.json").write_text("{}")
    script = tmp_path / "run.sh"
    script.write_text("cp /home/adam/foo /Users/bob/bar")
    out = loopskill_doctor(db, str(tmp_path))
    assert out["ok"] is False
    assert "run.sh" in out["hardcoded_paths"]
    hits = out["hardcoded_paths"]["run.sh"]
    assert any("/home/" in h for h in hits)
    assert any("/Users/" in h for h in hits)


def test_not_server_inspectable_preserves_input_path_verbatim(db):
    """Agents need the path echoed back so they can log/correlate."""
    p = "/Users/wisevision/.hermes/skills/critical-code-reviewer"
    out = loopskill_doctor(db, p)
    assert out["install_dir"] == p
