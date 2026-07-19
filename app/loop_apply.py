"""feat/member-loop-apply — client half: manifests → local Hermes cron jobs.

The last mile of the placement chain. The server half
(app/loop_assignment_routes.py) lets a member READ its assignments; this module
APPLIES them to the member's local Hermes scheduler (~/.hermes/cron/jobs.json)
so a loop deployed on LoopSkill becomes real, running crons on the agent.

Design contract (Adam 2026-07-17, subscriber framing):
  * The MANIFEST is the source of truth. Deployed crons are platform-managed:
    created, updated, and REMOVED as the assignment set changes. This is the
    GitOps model — you buy the loop AND its maintenance — not the
    install-once-orphan-forever pattern.
  * Managed jobs are namespaced: name = "loopskill/<loop_id>" and carry
    tags ["tier1", "loopskill-managed", "<loop_id>"]. The apply NEVER touches
    a job outside the namespace — a user's own crons are invisible to it.
  * Epoch-guarded: each managed job records the placement epoch it was built
    from. A stale-epoch assignment (epoch < recorded) is SKIPPED — placement
    moves are monotonic (see LoopPlacement CAS docstring).
  * Atomic: jobs.json is rewritten via tempfile + os.replace (a half-written
    file crashes every cron on the host). Same discipline as reconcile_client.

Pure functions + injected IO so the module is unit-testable without a live
scheduler; the ~/.hermes/scripts/loopskill-sync.sh cron calls apply_assignments
via python -m app.loop_apply_cli (follow-up wiring on the member host).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANAGED_PREFIX = "loopskill/"
MANAGED_TAG = "loopskill-managed"

# Accepted schedule forms — mirrors fleet_artifacts._SCHEDULE_RE ("cron-5-field
# or '<N>m|h' / 'every <N>h' shorthand"). Hermes cron accepts both natively.
_CRON_5FIELD = re.compile(r"^\s*(\S+\s+){4}\S+\s*$")
_SHORTHAND = re.compile(r"^(every\s+)?\d+\s*[mh]$", re.IGNORECASE)


@dataclass
class ApplyLoopsResult:
    """Outcome of one apply cycle over the managed-job namespace."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)  # [{loop_key, reason}]

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.removed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "removed": self.removed,
            "skipped": self.skipped,
            "changed": self.changed,
        }


def _valid_schedule(schedule: str | None) -> bool:
    if not schedule or not isinstance(schedule, str):
        return False
    s = schedule.strip()
    return bool(_CRON_5FIELD.match(s) or _SHORTHAND.match(s))


def _schedule_block(schedule: str) -> dict[str, Any]:
    """Project a manifest schedule string into the Hermes jobs.json shape.

    Live-found 2026-07-19 (first real deploy, 'atomic-habits' schedule='24h'):
    writing shorthand as {kind: 'cron', expr: '24h'} poisons every consumer
    that trusts kind — cron-watchdog fed '24h' to croniter and CRASHED its
    whole tick (CroniterBadCronError: 'Exactly 5, 6 or 7 columns...').
    Native Hermes shorthand jobs use {kind: 'interval', minutes: N, display:
    'every Nm'} — mirror that exactly; only genuine 5-field exprs get
    kind='cron'.
    """
    s = schedule.strip()
    m = _SHORTHAND.match(s)
    if m:
        num = int(re.sub(r"[^0-9]", "", s))
        minutes = num * 60 if s.lower().rstrip().endswith("h") else num
        return {
            "kind": "interval",
            "minutes": minutes,
            "display": f"every {num}{'h' if minutes == num * 60 else 'm'}",
        }
    return {"kind": "cron", "expr": s, "display": s}


def manifest_to_job(
    manifest: dict[str, Any],
    *,
    epoch: int,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one LoopManifest transport dict into a Hermes cron job dict.

    ``existing`` (the current managed job for this loop, if any) preserves
    scheduler-owned run-state fields (last_run_at, next_run_at, …) so an
    unchanged re-apply is a no-op and an update doesn't reset run history.
    """
    loop_id = str(manifest["loop_id"])
    now_iso = datetime.now(UTC).isoformat()
    schedule = str(manifest.get("schedule") or "").strip()
    prompt = str(manifest.get("prompt") or "")

    base: dict[str, Any] = (
        existing.copy()
        if existing
        else {
            "id": uuid.uuid4().hex[:12],
            "created_at": now_iso,
            "next_run_at": None,  # scheduler repopulates from the schedule expr
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_delivery_error": None,
            "paused_at": None,
            "paused_reason": None,
            "origin": None,
        }
    )

    model = manifest.get("model")
    base.update(
        {
            "name": f"{MANAGED_PREFIX}{loop_id}",
            "prompt": prompt,
            "skills": [
                s.get("id") for s in (manifest.get("skills") or []) if isinstance(s, dict) and s.get("id")
            ],
            "skill": None,
            "tags": ["tier1", MANAGED_TAG, loop_id],
            "model": model,
            "provider": None,
            "base_url": None,
            "script": None,
            "schedule": _schedule_block(schedule),
            "schedule_display": _schedule_block(schedule).get("display", schedule),
            "repeat": base.get("repeat") or {"times": None, "completed": 0},
            "enabled": bool(manifest.get("enabled", True)),
            "state": "scheduled",
            "deliver": manifest.get("deliver") or "local",
            # Provenance: which placement epoch built this job. Stale-epoch
            # assignments must never overwrite a newer apply.
            "loopskill": {"loop_id": loop_id, "epoch": epoch, "applied_at": now_iso},
        }
    )
    if base.get("skills"):
        base["skill"] = base["skills"][0]
    return base


def _job_desired_equal(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Compare only the DESIRED-state fields (ignore scheduler run-state)."""
    keys = ("name", "prompt", "skills", "schedule_display", "enabled", "deliver", "model")
    return all(existing.get(k) == desired.get(k) for k in keys)


def apply_assignments(
    assignments: list[dict[str, Any]],
    jobs_path: Path,
) -> ApplyLoopsResult:
    """Reconcile the managed-job namespace of ``jobs_path`` to ``assignments``.

    * assignment.manifest None → skipped (nothing to schedule; never fabricate).
    * invalid/lintable manifest (missing schedule/prompt) → skipped, loudly.
    * managed job whose loop_key is NOT in the assignment set → REMOVED
      (undeploy). Jobs outside the loopskill/ namespace are never touched.
    """
    result = ApplyLoopsResult()

    data: dict[str, Any] = {"jobs": []}
    if jobs_path.exists():
        data = json.loads(jobs_path.read_text() or '{"jobs": []}')
    jobs: list[dict[str, Any]] = data.get("jobs", [])

    managed = {
        j["name"][len(MANAGED_PREFIX) :]: j
        for j in jobs
        if isinstance(j.get("name"), str) and j["name"].startswith(MANAGED_PREFIX)
    }
    unmanaged = [
        j for j in jobs if not (isinstance(j.get("name"), str) and j["name"].startswith(MANAGED_PREFIX))
    ]

    desired_jobs: dict[str, dict[str, Any]] = {}
    for a in assignments:
        loop_key = str(a.get("loop_key") or "")
        manifest = a.get("manifest")
        epoch = int(a.get("epoch") or 0)
        if not loop_key:
            continue
        if manifest is None:
            result.skipped.append({"loop_key": loop_key, "reason": "no_manifest"})
            continue
        if not _valid_schedule(manifest.get("schedule")):
            result.skipped.append({"loop_key": loop_key, "reason": "invalid_schedule"})
            continue
        if not str(manifest.get("prompt") or "").strip():
            result.skipped.append({"loop_key": loop_key, "reason": "empty_prompt"})
            continue

        existing = managed.get(loop_key)
        if existing is not None:
            recorded_epoch = int((existing.get("loopskill") or {}).get("epoch") or 0)
            if epoch < recorded_epoch:
                # Stale assignment read (placement moved on) — keep current job.
                result.skipped.append({"loop_key": loop_key, "reason": "stale_epoch"})
                desired_jobs[loop_key] = existing
                continue

        job = manifest_to_job(manifest, epoch=epoch, existing=existing)
        desired_jobs[loop_key] = job
        if existing is None:
            result.created.append(loop_key)
        elif not _job_desired_equal(existing, job):
            result.updated.append(loop_key)

    # Undeploy: managed jobs no longer in the assignment set.
    for loop_key in managed:
        if loop_key not in desired_jobs:
            result.removed.append(loop_key)

    if result.changed:
        data["jobs"] = unmanaged + [desired_jobs[k] for k in sorted(desired_jobs)]
        data["updated_at"] = datetime.now(UTC).isoformat()
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(jobs_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, jobs_path)  # atomic — never leave jobs.json torn
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    return result
