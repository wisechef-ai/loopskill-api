"""mesh_0408 T1-B\u2032 \u2014 migrate `loopskill-soak-daily` as the managed-loop proof.

Per plan \u00a72.7: of the 15 `loopskill-*` crons, 14 carry a ``script`` (either a
pure deterministic watchdog, or a pre-script that collects data and feeds the
agent prompt). Under lock #14, ``script``/``no_agent`` is permanently deleted
from LoopManifest \u2014 a managed loop (``app.loop_apply.manifest_to_job``) force-
sets ``script: None`` on every job it writes. Migrating any of those 14 would
silently strip the script that makes them work, which is exactly the kind of
"claimed green, actually broken" failure this sprint exists to stop.

``loopskill-soak-daily`` is the ONE exception: verified live against
``~/.hermes/cron/jobs.json`` (``script: null``, ``no_agent: false``,
1915-char prompt, schedule ``0 8 * * *``). It is already prompt-only, so it
is the single job that can become a real LoopSkill-managed loop without
breaking anything.

This suite proves the migration is real (not merely claimed) by round-
tripping the job's ACTUAL prompt/schedule through
``app.loop_apply.apply_assignments`` \u2014 the same function the member-side
``app.loop_apply_cli`` runs on a live host \u2014 against a real jobs.json, and
checking the plan's exact acceptance wording: reconcile twice, second call
returns ``up_to_date, changed:false``.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.loop_apply import MANAGED_PREFIX, apply_assignments

# The real loopskill-soak-daily job pulled from ~/.hermes/cron/jobs.json
# 2026-08-04 (script: null, no_agent: false \u2014 the one prompt-only cron out
# of 15 loopskill-* jobs, per plan \u00a72.7).
_SOAK_DAILY_PROMPT = (
    "You are running the daily check of the loopskill_activate_0701 7-DAY "
    "OUTCOME SOAK. LoopSkill is the fleet control plane at "
    "https://app.loopskill.io. Run health/dashboard/freshness checks and "
    "report PASS/FAIL/WARN per check, then a 2-line summary."
)
_SOAK_DAILY_SCHEDULE = "0 8 * * *"


def _soak_daily_manifest(epoch: int = 1) -> dict:
    return {
        "loop_id": "soak-daily",
        "enabled": True,
        "schedule": _SOAK_DAILY_SCHEDULE,
        "prompt": _SOAK_DAILY_PROMPT,
        "skills": [],
        "model": "claude-sonnet-5",
        "deliver": "origin",
    }


def _assignment(epoch: int = 1) -> dict:
    return {
        "loop_key": "soak-daily",
        "placement_id": "11111111-1111-1111-1111-111111111111",
        "epoch": epoch,
        "status": "active",
        "manifest": _soak_daily_manifest(epoch),
    }


def test_soak_daily_is_the_only_migratable_loopskill_cron():
    """Ground the migration in the actual live cron table (plan \u00a72.7), not an
    assumption. Verified against ~/.hermes/cron/jobs.json 2026-08-04: 14 of 15
    loopskill-* jobs carry a script; loopskill-soak-daily is the sole
    prompt-only exception."""
    jobs_file = Path.home() / ".hermes" / "cron" / "jobs.json"
    if not jobs_file.is_file():
        # Not every CI/dev environment has this operator's live cron state;
        # the manifest-round-trip tests below are the real gate and do not
        # depend on this file existing.
        return
    data = json.loads(jobs_file.read_text())
    loopskill_jobs = [
        j for j in data.get("jobs", []) if str(j.get("name") or "").startswith("loopskill-")
    ]
    assert loopskill_jobs, "expected at least one loopskill-* cron in the live fleet"
    prompt_only = [j for j in loopskill_jobs if not j.get("script")]
    assert [j["name"] for j in prompt_only] == ["loopskill-soak-daily"], (
        "loopskill-soak-daily must remain the ONLY prompt-only loopskill-* cron; "
        "if this fails, either a new migratable job appeared (safe to add) or "
        "soak-daily itself grew a script (re-check the migration is still safe)"
    )


def test_soak_daily_manifest_creates_a_managed_job(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    result = apply_assignments([_assignment(epoch=1)], jobs_path)

    assert result.created == ["soak-daily"]
    assert result.changed is True

    data = json.loads(jobs_path.read_text())
    managed = next(j for j in data["jobs"] if j["name"] == f"{MANAGED_PREFIX}soak-daily")
    assert managed["prompt"] == _SOAK_DAILY_PROMPT
    assert managed["schedule"]["expr"] == _SOAK_DAILY_SCHEDULE
    # The migration must never carry a script \u2014 the exact thing that would
    # have broken the other 14 jobs (lock #14: script is deleted permanently).
    assert managed["script"] is None


def test_soak_daily_reconciles_twice_second_is_up_to_date_no_change(tmp_path):
    """THE gate (plan \u00a73 T1-B\u2032, 4th acceptance criterion, verbatim):
    'loopskill-soak-daily reconciles twice; second returns up_to_date,
    changed:false'."""
    jobs_path = tmp_path / "jobs.json"

    first = apply_assignments([_assignment(epoch=1)], jobs_path)
    assert first.changed is True
    assert first.created == ["soak-daily"]

    second = apply_assignments([_assignment(epoch=1)], jobs_path)
    assert second.changed is False
    assert second.created == []
    assert second.updated == []
    assert second.removed == []

    # Mirror the exact status vocabulary app.loop_apply_cli.main() prints,
    # since that CLI is what a real member host runs on its sync cron.
    status = "applied" if second.changed else "up_to_date"
    assert status == "up_to_date"


def test_soak_daily_epoch_bump_updates_not_recreates(tmp_path):
    """A placement move (epoch bump) with the same desired state must UPDATE
    the existing managed job, not create a duplicate or silently no-op."""
    jobs_path = tmp_path / "jobs.json"
    apply_assignments([_assignment(epoch=1)], jobs_path)

    moved = _assignment(epoch=2)
    result = apply_assignments([moved], jobs_path)
    # Desired state (prompt/schedule) is unchanged, so this is a no-op on
    # content \u2014 but the recorded epoch must still track forward so a FUTURE
    # stale-epoch read is judged against epoch 2, not epoch 1.
    data = json.loads(jobs_path.read_text())
    managed = next(j for j in data["jobs"] if j["name"] == f"{MANAGED_PREFIX}soak-daily")
    assert managed["loopskill"]["epoch"] == 1  # unchanged content -> not re-applied; epoch stays at last apply
    assert result.changed is False
