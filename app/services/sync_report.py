"""Sync-report ingestion service — activate_0701 Phase T.

Batched ingestion of fleet telemetry (loop runs, cron health, skill errors).
Kept in the services layer so the route module stays thin (600-line gate).

Key functions:
  - ingest_sync_report: caps, truncation, inserts. Returns (recorded, truncated).
  - rollup_loop_runs: idempotent daily UPSERT from raw LoopRun rows.
  - prune_raw: deletes LoopRun + CronHealthSnapshot past retention (30d).
  - cost_per_accepted_change: query helper reading rollups.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    CronHealthSnapshot,
    FleetMember,
    LoopRun,
    LoopRunDailyRollup,
    MemberLockfileSnapshot,
    SkillErrorReport,
)

logger = logging.getLogger(__name__)

# ── D9 size caps (from design contract §endpoint) ──────────────────────────

MAX_LOOP_RUNS = 200
MAX_SKILL_ERRORS = 100
MAX_CRON_FAILED = 50
MAX_FIELD_LEN = 2000
MAX_LOCKFILE_SKILLS = 500  # feat/fleet-console-state — cap per snapshot

# Server-side body cap (checked in the route handler before JSON parse).
MAX_BODY_BYTES = 256 * 1024  # 256 KB

# Retention window for raw telemetry rows.
DEFAULT_RETENTION_DAYS = 30


def _truncate(value: str | None, limit: int = MAX_FIELD_LEN) -> str | None:
    """Truncate a string to ``limit`` characters, preserving None."""
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit]


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime.

    Returns None for None/empty input. Tolerant of trailing 'Z' and
    offset-naive strings (assumes UTC).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Normalize trailing Z to +00:00 for fromisoformat compatibility.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def ingest_sync_report(
    db: Session,
    member: FleetMember,
    payload: dict[str, Any],
) -> tuple[dict[str, int | bool], dict[str, int]]:
    """Ingest one batched sync-report payload.

    Applies caps + truncation, inserts rows, bumps FleetMember.updated_at
    as the liveness marker. lockfile_state is NOT stored as rows (D9).

    Returns:
        (recorded, truncated) where recorded has the counts and truncated
        describes any dropped excess items.
    """
    now = datetime.now(UTC)
    truncated: dict[str, int] = {}

    # ── loop_runs ───────────────────────────────────────────────────────────
    loop_runs = payload.get("loop_runs") or []
    lr_truncated = 0
    if len(loop_runs) > MAX_LOOP_RUNS:
        lr_truncated = len(loop_runs) - MAX_LOOP_RUNS
        loop_runs = loop_runs[:MAX_LOOP_RUNS]
    if lr_truncated:
        truncated["loop_runs"] = lr_truncated

    for lr in loop_runs:
        db.add(
            LoopRun(
                member_id=member.id,
                fleet_id=member.fleet_id,
                loop_slug=(lr.get("loop_slug") or "")[:255],
                instance_key=(lr.get("instance_key") or "")[:255],
                outcome=(lr.get("outcome") or "failure")[:32],
                accepted_change=bool(lr.get("accepted_change", False)),
                cost_usd=lr.get("cost_usd"),
                duration_seconds=lr.get("duration_seconds"),
                provenance_id=(lr.get("provenance_id") or None)[:64] if lr.get("provenance_id") else None,
                started_at=_parse_dt(lr.get("started_at")),
                detail=_truncate(lr.get("detail")),
            )
        )

    # ── skill_errors ─────────────────────────────────────────────────────────
    skill_errors = payload.get("skill_errors") or []
    se_truncated = 0
    if len(skill_errors) > MAX_SKILL_ERRORS:
        se_truncated = len(skill_errors) - MAX_SKILL_ERRORS
        skill_errors = skill_errors[:MAX_SKILL_ERRORS]
    if se_truncated:
        truncated["skill_errors"] = se_truncated

    for se in skill_errors:
        db.add(
            SkillErrorReport(
                member_id=member.id,
                fleet_id=member.fleet_id,
                slug=(se.get("slug") or "")[:255],
                semver=(se.get("semver") or None)[:32] if se.get("semver") else None,
                signature=(se.get("signature") or "")[:65535],
                summary=_truncate(se.get("summary")) or "",
            )
        )

    # ── cron_health ──────────────────────────────────────────────────────────
    cron_health = payload.get("cron_health")
    cron_stored = False
    if cron_health and isinstance(cron_health, dict):
        failed_list = cron_health.get("failed") or []
        ch_truncated = 0
        if len(failed_list) > MAX_CRON_FAILED:
            ch_truncated = len(failed_list) - MAX_CRON_FAILED
            failed_list = failed_list[:MAX_CRON_FAILED]
        if ch_truncated:
            truncated["cron_health_failed"] = ch_truncated

        counts = cron_health.get("counts") or {}
        db.add(
            CronHealthSnapshot(
                member_id=member.id,
                fleet_id=member.fleet_id,
                failed=failed_list,
                total_count=int(counts.get("total", 0)),
                ok_count=int(counts.get("ok", 0)),
                error_count=int(counts.get("error", 0)),
            )
        )
        cron_stored = True

    # ── lockfile_state (feat/fleet-console-state) ────────────────────────────
    # ONE latest-state row per member, upserted — O(fleet size) not O(time),
    # so this stays within the D9 data-efficiency posture. Answers "what is
    # actually installed on this agent right now" for the fleet console.
    lockfile_state = payload.get("lockfile_state")
    lockfile_stored = False
    if isinstance(lockfile_state, list):
        capped = lockfile_state[:MAX_LOCKFILE_SKILLS]
        if len(lockfile_state) > MAX_LOCKFILE_SKILLS:
            truncated["lockfile_state"] = len(lockfile_state) - MAX_LOCKFILE_SKILLS
        clean = [
            {
                "slug": (s.get("slug") or "")[:255],
                "pinned_version": (s.get("pinned_version") or None),
                # Agents' collector scripts ship the checksum as "sha256";
                # the raw lockfile field is "checksum_sha256". Accept both.
                "checksum_sha256": (s.get("checksum_sha256") or s.get("sha256") or None),
            }
            for s in capped
            if isinstance(s, dict) and s.get("slug")
        ]
        snap = db.query(MemberLockfileSnapshot).filter(MemberLockfileSnapshot.member_id == member.id).first()
        if snap is None:
            db.add(
                MemberLockfileSnapshot(
                    member_id=member.id,
                    fleet_id=member.fleet_id,
                    skills=clean,
                    cycle_ts=(payload.get("cycle_ts") or None),
                )
            )
        else:
            snap.skills = clean
            snap.cycle_ts = payload.get("cycle_ts") or None
            snap.reported_at = now
        lockfile_stored = True

    # Bump FleetMember.updated_at as the liveness marker (drift computation
    # stays in the reconcile endpoint; the snapshot above is the console's
    # actual-state read surface).
    member.updated_at = now

    db.commit()

    recorded: dict[str, int | bool] = {
        "loop_runs": len(loop_runs),
        "skill_errors": len(skill_errors),
        "cron_health": cron_stored,
        "lockfile_state": lockfile_stored,
    }
    return recorded, truncated


def rollup_loop_runs(db: Session, day: date | None = None) -> int:
    """Aggregate all LoopRun rows for ``day`` into LoopRunDailyRollup.

    Idempotent: re-running for the same day produces the same aggregates.
    Uses delete-then-insert in SQLite (test path); the unique constraint
    ensures correctness on Postgres via ON CONFLICT when available.

    Returns the number of rollup rows written.
    """
    if day is None:
        day = date.today()

    # Find all distinct (fleet_id, member_id, loop_slug) groups for this day.
    # We compute the date from LoopRun.created_at (timezone-aware).
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    # Delete existing rollups for this day so re-insertion is idempotent.
    db.query(LoopRunDailyRollup).filter(LoopRunDailyRollup.day == day).delete()

    # Aggregate from raw rows — load grouped rows, then iterate to compute
    # all aggregates per (fleet, member, loop_slug).
    groups = (
        db.query(LoopRun.fleet_id, LoopRun.member_id, LoopRun.loop_slug)
        .filter(LoopRun.created_at >= day_start, LoopRun.created_at < day_end)
        .group_by(LoopRun.fleet_id, LoopRun.member_id, LoopRun.loop_slug)
        .all()
    )

    count = 0
    for fleet_id, member_id, loop_slug in groups:
        # Compute successes/failures/cost/duration in a second pass for this group.
        group_rows = (
            db.query(LoopRun)
            .filter(
                LoopRun.fleet_id == fleet_id,
                LoopRun.member_id == member_id,
                LoopRun.loop_slug == loop_slug,
                LoopRun.created_at >= day_start,
                LoopRun.created_at < day_end,
            )
            .all()
        )
        successes = sum(1 for r in group_rows if r.outcome in ("success", "budget_stop", "max_turns_stop"))
        failures = sum(1 for r in group_rows if r.outcome == "failure")
        accepted_changes = sum(1 for r in group_rows if r.accepted_change)
        cost_total = sum(float(r.cost_usd or 0) for r in group_rows)
        duration_total = sum(r.duration_seconds or 0 for r in group_rows)

        db.add(
            LoopRunDailyRollup(
                fleet_id=fleet_id,
                member_id=member_id,
                loop_slug=loop_slug,
                day=day,
                runs=len(group_rows),
                successes=successes,
                failures=failures,
                accepted_changes=accepted_changes,
                cost_usd_total=round(cost_total, 4) if cost_total else None,
                duration_seconds_total=duration_total or None,
            )
        )
        count += 1

    db.commit()
    return count


def prune_raw(db: Session, older_than_days: int = DEFAULT_RETENTION_DAYS) -> dict[str, int]:
    """Delete LoopRun + CronHealthSnapshot rows past retention.

    NEVER touches rollups. Returns counts of deleted rows per table.
    """
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)

    loop_run_count = db.query(LoopRun).filter(LoopRun.created_at < cutoff).delete()
    cron_count = db.query(CronHealthSnapshot).filter(CronHealthSnapshot.created_at < cutoff).delete()
    db.commit()

    logger.info(
        "sync-report prune: deleted %d loop_runs, %d cron_health_snapshots (older than %d days)",
        loop_run_count,
        cron_count,
        older_than_days,
    )
    return {"loop_runs": loop_run_count, "cron_health_snapshots": cron_count}


def cost_per_accepted_change(
    db: Session,
    fleet_id: UUID,
    loop_slug: str | None = None,
    days: int = 30,
) -> float | None:
    """Query helper: cost per accepted change from rollup tables.

    Returns sum(cost_usd_total) / nullif(sum(accepted_changes), 0), or None
    if there are zero accepted changes.
    """
    since = date.today() - timedelta(days=days)
    q = db.query(
        func.sum(LoopRunDailyRollup.cost_usd_total),
        func.sum(LoopRunDailyRollup.accepted_changes),
    ).filter(
        LoopRunDailyRollup.fleet_id == fleet_id,
        LoopRunDailyRollup.day >= since,
    )
    if loop_slug is not None:
        q = q.filter(LoopRunDailyRollup.loop_slug == loop_slug)

    total_cost, total_accepted = q.first()
    if total_accepted is None or total_accepted == 0:
        return None
    if total_cost is None:
        return 0.0
    return float(total_cost) / float(total_accepted)
