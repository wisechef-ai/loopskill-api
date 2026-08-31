"""The lockfile — a portable, diffable snapshot of skills installed locally.

Two machines that have never talked to each other, or to us, can each run
``loopskill import`` and diff the resulting files with plain ``diff -u`` or
``loopskill diff``. Stable, sorted, versioned JSON is the whole contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loopskill.scanner import ClientScan

#: Bump when the on-disk shape changes. ``diff`` refuses to compare across
#: incompatible major versions rather than silently misreading fields.
LOCKFILE_VERSION = 1


def build_lockfile(scans: list[ClientScan]) -> dict[str, Any]:
    """Build the lockfile dict from a list of per-client scans.

    Deterministic: clients sorted by id, skills sorted by id (scanner already
    sorts skills; this re-sorts defensively so callers passing raw lists
    still get a stable file). No timestamps, no host identifiers — anything
    that would make two logically-identical machines diff as different is
    deliberately left out of the comparable payload.
    """
    clients: dict[str, Any] = {}
    for scan in sorted(scans, key=lambda s: s.client.client_id):
        clients[scan.client.client_id] = {
            "root": str(scan.client.path),
            "installed": scan.client.exists,
            "skill_count": len(scan.skills),
            "skills": [
                {
                    "id": s.skill_id,
                    "sha256": s.sha256,
                    "size_bytes": s.size_bytes,
                    "name": s.name,
                    "description": s.description,
                }
                for s in sorted(scan.skills, key=lambda r: r.skill_id)
            ],
        }
    return {"lockfile_version": LOCKFILE_VERSION, "clients": clients}


def dumps(lockfile: dict[str, Any]) -> str:
    """Render a lockfile dict as stable, sorted, human-readable JSON."""
    return json.dumps(lockfile, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def loads(text: str) -> dict[str, Any]:
    """Parse lockfile JSON text back into a dict."""
    data = json.loads(text)
    if not isinstance(data, dict) or "lockfile_version" not in data:
        raise ValueError("Not a loopskill lockfile: missing 'lockfile_version'")
    return data


def load_path(path: Path) -> dict[str, Any]:
    """Read and parse a lockfile from disk."""
    return loads(path.read_text(encoding="utf-8"))
