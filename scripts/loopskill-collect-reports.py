#!/usr/bin/env python3
"""LoopSkill sync-report collector — the agent-side telemetry drain.

activate_0701 Phase T client half, packaged for a stranger's host (converge_0208
P4). Collects everything accumulated since the last cycle and POSTs it as ONE
batched ``/api/sync-report``:

  - ``loop_runs``     — spool files written by ``loopskill-emit-run.sh``
  - ``cron_health``   — failures + counts parsed from the host scheduler
  - ``lockfile_state``— what is actually installed on this agent right now

Sent spool files move to ``outbox/.sent/`` (at-least-once semantics: a POST that
succeeds but whose response is lost re-sends next cycle rather than dropping the
record).

**Always exits 0.** This runs from cron next to real work; a telemetry drain that
returns non-zero flips the host's own watchdog and pages a human about nothing.
Errors print to stdout for the cron log and retry on the next cycle.

Member key resolution, in order:
  1. ``$LOOPSKILL_MEMBER_KEY``
  2. ``$RECIPES_API_KEY``
  3. ``$LOOPSKILL_SECRETS`` / ``~/.hermes/secrets/loopskill_tori.json`` —
     ``member_api_key_plain_b64``

Paths are overridable by environment so this works on hosts that are not Hermes:
``LOOPSKILL_API``, ``LOOPSKILL_OUTBOX``, ``LOOPSKILL_CRON_STATE``,
``LOOPSKILL_LOCKFILE``, ``LOOPSKILL_SKILLS_DIR``.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

API = os.environ.get("LOOPSKILL_API", "https://app.loopskill.io")
HERMES = Path.home() / ".hermes"

SECRETS = Path(os.environ.get("LOOPSKILL_SECRETS", HERMES / "secrets" / "loopskill_tori.json"))
OUTBOX = Path(os.environ.get("LOOPSKILL_OUTBOX", HERMES / "loopskill" / "outbox"))
SENT = OUTBOX / ".sent"
CRON_STATE = Path(os.environ.get("LOOPSKILL_CRON_STATE", HERMES / "cron" / "jobs.json"))
LOCKFILE = Path(
    os.environ.get("LOOPSKILL_LOCKFILE", HERMES / "loopskill" / "state" / "loopskill-lock.json")
)
SKILLS_DIR = Path(os.environ.get("LOOPSKILL_SKILLS_DIR", HERMES / "skills"))

# Server-side caps (app/services/sync_report.py) — respected client-side so a
# backlog is drained in bounded batches instead of being silently truncated.
MAX_LOOP_RUNS = 200
MAX_LOCKFILE_SKILLS = 500


def member_key() -> str | None:
    """Resolve the FLEET MEMBER key. Loop telemetry is a member surface."""
    for var in ("LOOPSKILL_MEMBER_KEY", "RECIPES_API_KEY"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        d = json.loads(SECRETS.read_text())
        b64 = d.get("member_api_key_plain_b64")
        return base64.b64decode(b64).decode() if b64 else None
    except (OSError, ValueError, KeyError):
        return None


def collect_loop_runs() -> tuple[list[dict], list[Path]]:
    """Read the spool `loopskill-emit-run.sh` writes. Unparseable files are
    left in place rather than dropped — a partial write completes next cycle."""
    runs: list[dict] = []
    files: list[Path] = []
    if not OUTBOX.is_dir():
        return runs, files
    for f in sorted(OUTBOX.glob("*.json"))[:MAX_LOOP_RUNS]:
        try:
            runs.append(json.loads(f.read_text()))
            files.append(f)
        except (OSError, ValueError):
            continue
    return runs, files


def collect_cron_health() -> dict | None:
    try:
        jobs = json.loads(CRON_STATE.read_text())
    except (OSError, ValueError):
        return None
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs", [])
    if not isinstance(jobs, list):
        return None
    failed, ok = [], 0
    for j in jobs:
        if not isinstance(j, dict):
            continue
        if j.get("last_status") == "error":
            failed.append(
                {
                    "job_name": str(j.get("name") or j.get("id", "?"))[:100],
                    "last_status": "error",
                    "consecutive_failures": int(j.get("consecutive_failures") or 1),
                }
            )
        else:
            ok += 1
    return {"failed": failed[:50], "counts": {"total": len(jobs), "ok": ok, "error": len(failed)}}


def lockfile_state() -> list[dict]:
    """Full installed-state picture: loopskill-managed skills (from the lockfile,
    versioned + checksummed) PLUS the agent's local skill library (hand-built
    skills never deployed through LoopSkill). The local ones are the HARVEST
    CANDIDATES the fleet console surfaces ("built on this agent, in no bundle —
    promote it?"). Lockfile wins on slug collision."""
    entries: dict[str, dict] = {}
    try:
        lock = json.loads(LOCKFILE.read_text())
        for s in lock.get("skills", []):
            entries[s["slug"]] = {
                "slug": s["slug"],
                "pinned_version": s.get("pinned_version"),
                "sha256": s.get("sha256"),
            }
    except (OSError, ValueError, KeyError):
        pass
    if SKILLS_DIR.is_dir():
        try:
            found = sorted(SKILLS_DIR.glob("*/SKILL.md")) + sorted(SKILLS_DIR.glob("*/*/SKILL.md"))
            for md in found:
                slug = md.parent.name
                if slug not in entries:
                    entries[slug] = {"slug": slug, "pinned_version": None, "sha256": None}
        except OSError:
            pass
    return list(entries.values())[:MAX_LOCKFILE_SKILLS]


def build_payload() -> tuple[dict, list[Path], dict | None]:
    runs, run_files = collect_loop_runs()
    cron = collect_cron_health()
    payload: dict = {"cycle_ts": datetime.now(UTC).isoformat()}
    state = lockfile_state()
    if state:
        payload["lockfile_state"] = state
    if runs:
        payload["loop_runs"] = runs
    if cron is not None:
        payload["cron_health"] = cron
    return payload, run_files, cron


def main() -> int:
    key = member_key()
    if not key:
        print("sync-report: no member key (set LOOPSKILL_MEMBER_KEY or enroll first) — skipping")
        return 0

    payload, run_files, cron = build_payload()

    req = urllib.request.Request(
        f"{API.rstrip('/')}/api/sync-report",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310  # Rationale: fixed https API base, member-key auth.
            result = json.loads(resp.read().decode())
    # Rationale: watchdog pattern — a telemetry drain must never fail its cron.
    except Exception as exc:  # noqa: BLE001
        print(f"sync-report POST failed (retry next cycle): {exc}")
        return 0

    recorded = result.get("recorded", {})
    if run_files:
        SENT.mkdir(parents=True, exist_ok=True)
        for f in run_files:
            try:
                f.rename(SENT / f.name)
            except OSError:
                pass
    n_runs = recorded.get("loop_runs", 0)
    if n_runs or (cron and cron["counts"]["error"]):
        print(f"sync-report: {n_runs} loop_runs, cron errors={cron['counts']['error'] if cron else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
