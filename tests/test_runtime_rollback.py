"""F.6 — atomic rollback: snapshot + revert end-to-end against tmpdir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runtime import rollback
from runtime.adapters.base import skill_root, runtime_root


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RECIPES_RUNTIME_ROOT", str(tmp_path / "rr"))
    return tmp_path / "rr"


def test_snapshot_empty_skill(runtime_dir):
    snap = rollback.snapshot("brand-new")
    assert snap.skill_slug == "brand-new"
    assert snap.file_list == []
    assert snap.snapshot_dir.exists()


def test_snapshot_and_revert_restores_files(runtime_dir):
    sk = skill_root("demo")
    (sk / "marker.txt").write_text("v1")
    (sk / "bin").mkdir(exist_ok=True)
    (sk / "bin" / "uv").write_bytes(b"v1-binary")

    snap = rollback.snapshot("demo")
    assert "marker.txt" in snap.file_list

    # Mutate after snapshot — pretend an install half-finished.
    (sk / "marker.txt").write_text("v2-broken")
    (sk / "bin" / "uv").write_bytes(b"v2-broken")
    (sk / "bin" / "extra").write_bytes(b"newly-added")

    rollback.revert_filesystem(snap)

    assert (sk / "marker.txt").read_text() == "v1"
    assert (sk / "bin" / "uv").read_bytes() == b"v1-binary"
    assert not (sk / "bin" / "extra").exists()


def test_teardown_handles_swallows_errors(runtime_dir):
    from runtime.services.base import ServiceHandle
    from runtime.cron.base import CronHandle

    bad_service = ServiceHandle(name="x", backend="nonexistent")
    bad_cron = CronHandle(name="y", backend="nonexistent",
                          schedule="0 0 * * *", cmd="true")

    torn = rollback.teardown_handles([bad_service], [bad_cron])
    assert torn == {}


def test_full_rollback_round_trip(runtime_dir):
    sk = skill_root("rt")
    (sk / "state").write_text("clean")
    snap = rollback.snapshot("rt")

    (sk / "state").write_text("dirty-from-install")

    result = rollback.rollback("rt", snap, [], [])
    assert result["reverted"]
    assert (sk / "state").read_text() == "clean"
