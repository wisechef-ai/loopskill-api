"""Atomic rollback (Phase F.6).

Pre-install: snapshot filesystem markers under ``~/.recipes/runtime/``
so we can revert to the prior on-disk state if any later step fails.

On failure:
  * filesystem reverts via ``revert_filesystem``
  * services / crons created during the install are torn down (caller
    passes the handles it accumulated)
  * the install_events row is updated to status='rolled_back'

The DB hook is intentionally narrow — it takes a SQLAlchemy session and
the install_event id, so callers control transaction boundaries.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from runtime.adapters.base import runtime_root
from runtime.cron.base import CronHandle
from runtime.services.base import ServiceHandle


@dataclass
class Snapshot:
    skill_slug: str
    timestamp: int
    snapshot_dir: Path
    file_list: list[str] = field(default_factory=list)


def snapshots_root() -> Path:
    p = runtime_root() / "snapshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def snapshot(skill_slug: str, *, _now=time.time) -> Snapshot:
    """Copy the current ``~/.recipes/runtime/<slug>/`` tree into snapshots/.

    No-op (empty file list) when the per-skill dir doesn't yet exist —
    that's the common case for first-time installs.
    """
    ts = int(_now())
    snap_dir = snapshots_root() / f"{skill_slug}-{ts}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    src = runtime_root() / skill_slug
    files: list[str] = []
    if src.exists():
        shutil.copytree(src, snap_dir / "tree", dirs_exist_ok=True)
        for p in (snap_dir / "tree").rglob("*"):
            if p.is_file():
                files.append(str(p.relative_to(snap_dir / "tree")))

    manifest = {"skill_slug": skill_slug, "timestamp": ts, "files": files}
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return Snapshot(skill_slug=skill_slug, timestamp=ts,
                    snapshot_dir=snap_dir, file_list=files)


def revert_filesystem(snap: Snapshot) -> bool:
    """Replace ``~/.recipes/runtime/<slug>/`` with the snapshot's tree.

    If the snapshot recorded no tree (i.e. the install was first-time),
    we wipe the per-skill dir entirely.
    """
    target = runtime_root() / snap.skill_slug
    if target.exists():
        shutil.rmtree(target)

    snap_tree = snap.snapshot_dir / "tree"
    if snap_tree.exists():
        shutil.copytree(snap_tree, target)
    return True


def teardown_handles(service_handles: Iterable[ServiceHandle],
                     cron_handles: Iterable[CronHandle]) -> dict[str, list[str]]:
    """Best-effort uninstall of services/crons added during this attempt.

    Returns a map of ``backend → [handle_name]`` for what was torn down.
    Errors during teardown are swallowed: rollback is best-effort by design.
    """
    torn: dict[str, list[str]] = {}

    for h in service_handles:
        try:
            mod = _service_module(h.backend)
            if mod and hasattr(mod, "teardown"):
                mod.teardown(h)
                torn.setdefault(h.backend, []).append(h.name)
        except Exception:
            pass

    for h in cron_handles:
        try:
            mod = _cron_module(h.backend)
            if mod and hasattr(mod, "unregister"):
                mod.unregister(h)
                torn.setdefault(h.backend, []).append(h.name)
        except Exception:
            pass

    return torn


def _service_module(backend: str):
    if backend == "docker-compose":
        from runtime.services import docker_compose
        return docker_compose
    if backend == "systemd-user":
        from runtime.services import systemd_user
        return systemd_user
    if backend == "launchd":
        from runtime.services import launchd
        return launchd
    return None


def _cron_module(backend: str):
    if backend == "hermes":
        from runtime.cron import hermes
        return hermes
    if backend == "systemd-timer":
        from runtime.cron import systemd_timer
        return systemd_timer
    if backend == "launchd":
        from runtime.cron import launchd
        return launchd
    if backend == "windows-task":
        from runtime.cron import windows_task
        return windows_task
    return None


def mark_rolled_back(session: Any, install_event_id: str) -> None:
    """Update the install_events row for this attempt to status='rolled_back'.

    Caller owns commit semantics — we only set the column and flush.
    """
    from app.models import InstallEvent  # local import to avoid hard dep at module load
    row = session.get(InstallEvent, install_event_id)
    if row is not None:
        row.status = "rolled_back"
        session.flush()


def rollback(skill_slug: str, snap: Snapshot,
             service_handles: Iterable[ServiceHandle],
             cron_handles: Iterable[CronHandle],
             *, session: Any | None = None,
             install_event_id: str | None = None) -> dict[str, Any]:
    """End-to-end rollback: filesystem + handles + DB marker."""
    revert_filesystem(snap)
    torn = teardown_handles(service_handles, cron_handles)
    if session is not None and install_event_id is not None:
        mark_rolled_back(session, install_event_id)
    return {"reverted": True, "skill_slug": skill_slug, "torn": torn,
            "snapshot": str(snap.snapshot_dir)}
