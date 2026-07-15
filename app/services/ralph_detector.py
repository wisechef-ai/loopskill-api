"""spotify_1507 Phase F — Ralph-loop detector.

"Ralph" (after Ralph Wiggum): an agent stuck re-running the SAME loop over and
over with no forward progress — same loop_slug + instance_key, repeated
outcomes, zero accepted_change. Left undetected it burns cost and never
converges. The fleet pane must surface it so an operator sees "this member is
spinning" without reading raw LoopRun rows.

Signal (from the LoopRun telemetry, Phase E): within a recent window, a
(member_id, loop_slug, instance_key) triple with:
  - run_count >= min_runs (default 5), AND
  - accepted_change count == 0 (no run ever changed anything), AND
  - a dominant repeated outcome (>= repeat_ratio of runs share one outcome)
= a Ralph loop. The dominant-outcome test distinguishes a genuinely-stuck loop
from one that's legitimately retrying through varied transient states.

Pure function over rows (detect_ralph_loops) + a DB query wrapper
(find_ralph_loops) so the pane route and the unit tests share one logic path.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LoopRun


def detect_ralph_loops(
    runs: list[dict[str, Any]],
    *,
    min_runs: int = 5,
    repeat_ratio: float = 0.8,
) -> list[dict[str, Any]]:
    """Pure detector over LoopRun-shaped dicts.

    Each run dict needs: member_id, loop_slug, instance_key, outcome,
    accepted_change (bool). Returns one finding per stuck triple:
      {member_id, loop_slug, instance_key, run_count, dominant_outcome,
       dominant_count, accepted_changes}

    A triple is a Ralph loop iff:
      run_count >= min_runs AND accepted_changes == 0 AND the most-common
      outcome accounts for >= repeat_ratio of the runs.
    """
    # Group by (member, loop, instance).
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        key = (r.get("member_id"), r.get("loop_slug"), r.get("instance_key"))
        groups[key].append(r)

    findings: list[dict[str, Any]] = []
    for (member_id, loop_slug, instance_key), grp in groups.items():
        run_count = len(grp)
        if run_count < min_runs:
            continue
        accepted = sum(1 for r in grp if r.get("accepted_change"))
        if accepted > 0:
            continue  # progress was made — not stuck
        # Dominant outcome.
        outcome_counts: dict[str, int] = defaultdict(int)
        for r in grp:
            outcome_counts[r.get("outcome") or "unknown"] += 1
        dominant_outcome, dominant_count = max(outcome_counts.items(), key=lambda kv: kv[1])
        if dominant_count / run_count < repeat_ratio:
            continue  # varied outcomes — legitimately churning, not Ralph
        findings.append(
            {
                "member_id": member_id,
                "loop_slug": loop_slug,
                "instance_key": instance_key,
                "run_count": run_count,
                "dominant_outcome": dominant_outcome,
                "dominant_count": dominant_count,
                "accepted_changes": accepted,
            }
        )
    # Stable order: worst offenders (most runs) first.
    findings.sort(key=lambda f: (-f["run_count"], str(f["loop_slug"])))
    return findings


def find_ralph_loops(
    db: Session,
    fleet_id,
    *,
    window_hours: int = 24,
    min_runs: int = 5,
    repeat_ratio: float = 0.8,
) -> list[dict[str, Any]]:
    """DB wrapper: pull recent LoopRuns for a fleet and run the detector."""
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = db.execute(
        select(
            LoopRun.member_id,
            LoopRun.loop_slug,
            LoopRun.instance_key,
            LoopRun.outcome,
            LoopRun.accepted_change,
        ).where(LoopRun.fleet_id == fleet_id, LoopRun.created_at >= since)
    ).all()
    runs = [
        {
            "member_id": str(r.member_id),
            "loop_slug": r.loop_slug,
            "instance_key": r.instance_key,
            "outcome": r.outcome,
            "accepted_change": bool(r.accepted_change),
        }
        for r in rows
    ]
    return detect_ralph_loops(runs, min_runs=min_runs, repeat_ratio=repeat_ratio)
