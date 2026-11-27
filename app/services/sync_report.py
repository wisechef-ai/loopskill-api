"""Sync-report ingestion service — activate_0701 Phase T.

Batched ingestion of fleet telemetry (loop runs, cron health, lockfile state).
Kept in the services layer so the route module stays thin (600-line gate).

RETIRED (t_a7ebcc96, 2026-11-16): the `skill_errors` section is no longer
ingested. It was the ONLY writer of skill_error_reports, and the fleet client
never sent the key — scripts/loopskill-collect-reports.py:build_payload() builds
cycle_ts / lockfile_state / loop_runs / cron_health and nothing else.

The emptiness was structural, not an outage. This endpoint stayed demonstrably
healthy the whole time: 22,834 accepted sync cycles between 2026-07-02 and
2026-11-16 (~167/day across the 3 live members; count of cron_health_snapshots,
prod, measured 2026-11-16 — the newest row landed 7 minutes before that
measurement), and skill_error_reports still held 0 rows. A field no client
produces cannot fill a table no matter how healthy the ingest is.

The request field is still ACCEPTED (ignored) for wire compatibility; see
app/sync_report_routes.py. Skill errors agents actually report are IncidentReport
rows written by POST /api/v1/skill-error or MCP loopskill_report_skill_error —
i.e. rows matching IncidentReport.skill_error_rows(). Do NOT read "IncidentReport"
as one population when counting them: POST /api/feedback/incident writes the SAME
table with report_type='incident', and those are crash reports of LoopSkill's own
tooling, not skill errors (t_0111f6c4, 2026-11-16; see the IncidentReport class
docstring).

LIVENESS WRITER (t_5734546c, 2026-11-27): ingest_sync_report also stamps
fleet_member_liveness.last_ping_at — the member's operational ping. That column
previously had one writer, the owner-gated loopskill_ping MCP tool, which no
client calls: prod sat at 2026-07-16 for all three live members while this
endpoint proved them alive every cycle. Same diagnosis as the skill_errors
retirement above, opposite verdict — there the writer read a key no client ever
built, so the rail was dead end-to-end; here the producer already exists, is
member-key authenticated, and fires every cycle, so the honest fix is to route
the ping where the life already flows. provides{} stays owner-authoritative
(loopskill_ping); see _stamp_member_liveness.

Key functions:
  - ingest_sync_report: caps, truncation, inserts. Returns (recorded, truncated).
  - rollup_loop_runs: idempotent, NON-destructive daily aggregate from raw rows.
  - prune_raw: deletes LoopRun + CronHealthSnapshot past retention (30d). Mechanism
    only — see its docstring for why nothing in ``app/`` may call it directly.
  - uncovered_day_groups: the prune's precondition (rollup must exist per day-group).
  - raw_counts_before: read-only preview of what a prune at this cutoff removes.
  - cost_per_accepted_change: query helper reading rollups.

RETENTION IS SCHEDULED, NOT ASSUMED (t_28f34a8a, 2026-11-16). This module's prose
says raw rows are pruned at 30d and the rollup docstrings lean on that ("carried
onto the rollup because raw rows are pruned at 30d"). In prod it had never been
true: 137 days of raw ``loop_runs`` + ``cron_health_snapshots`` were on disk and
``loop_run_daily_rollups`` held 2 rows for 46 raw day-groups. A retention window
nobody schedules is a promise with no implementation, so the schedule now lives in
one place — ``app/crons/sync_report_pruner.py`` (+ ``deploy/crontab.sync-report-retention``)
— and the prune refuses to run without rollup coverage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    CronHealthSnapshot,
    FleetMember,
    FleetMemberLiveness,
    LoopRun,
    LoopRunDailyRollup,
    MemberLockfileSnapshot,
)
from app.services.synthetic_runs import (
    SELF_ORIGINATED_LOOP_SLUGS,
    RunCounts,
    origin_verdict_for_member,
)

logger = logging.getLogger(__name__)

# ── D9 size caps (from design contract §endpoint) ──────────────────────────

MAX_LOOP_RUNS = 200
MAX_CRON_FAILED = 50
MAX_FIELD_LEN = 2000
MAX_LOCKFILE_SKILLS = 500  # feat/fleet-console-state — cap per snapshot

# Server-side body cap (checked in the route handler before JSON parse).
MAX_BODY_BYTES = 256 * 1024  # 256 KB

# t_5734546c (2026-11-27): the collector's cadence — the deployed crontab runs
# loopskill-collect-reports.py every 30 minutes, so a liveness row created by a
# sync-report (rather than by an owner ping) declares this interval. The
# stale-member alert flags at STALE_MULTIPLE (3x) of the declared interval; at
# the model default (300s) a */30 collector would read as stale 15 minutes
# into every cycle. 1800s x 3 = 90 min tolerates two missed cycles, matching
# the alert's stated intent (stale_member_alert.py).
SYNC_REPORT_RECONCILE_INTERVAL_SECONDS = 1800


def _stamp_member_liveness(db: Session, member: FleetMember, now: datetime) -> str:
    """Upsert the member's operational ping — an accepted sync-report IS one.

    t_5734546c: ``fleet_member_liveness.last_ping_at`` previously had exactly
    one writer — the owner-gated ``loopskill_ping`` MCP tool, which no client
    calls — so the column sat at 2026-07-16 while THIS path proved the members
    alive every cycle (``fleet_members.updated_at`` stayed current). The
    sync-report POST is member-key authenticated and fires on every collector
    cycle, which makes it the natural writer; no client change is required.

    Creation stamps ``reconcile_interval_seconds`` with the collector cadence
    so the stale-member alert's 3x threshold is honest for */30 members. The
    owner's ``provides{}`` advertisement and any owner-declared interval are
    NEVER touched here: an ingest must not fabricate capabilities, and the
    owner ping stays authoritative for both.

    Returns ``"created"`` or ``"refreshed"`` so the ack can report the ping
    honestly instead of asserting a constant the write path cannot falsify.
    """
    row = db.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == member.id).first()
    if row is None:
        db.add(
            FleetMemberLiveness(
                member_id=member.id,
                last_ping_at=now,
                reconcile_interval_seconds=SYNC_REPORT_RECONCILE_INTERVAL_SECONDS,
            )
        )
        return "created"
    row.last_ping_at = now
    return "refreshed"


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
) -> tuple[dict[str, int | bool | str], dict[str, int]]:
    """Ingest one batched sync-report payload.

    Applies caps + truncation, inserts rows, bumps FleetMember.updated_at
    as the liveness marker, and stamps the member's operational ping in
    fleet_member_liveness (t_5734546c — an accepted report IS a ping).
    lockfile_state is NOT stored as rows (D9).

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

    # mesh_0408 W4: resolve the IDENTITY's verdict ONCE per report (the
    # member/fleet/key markers are constant across the batch) and fall back to
    # the per-slug backstop only when nobody has classified this identity.
    # Frozen onto each row so counts never need a join.
    #
    # W4b: this is a precedence, NOT an OR. Or-ing the slug in unconditionally
    # meant an explicitly-external customer who declared `p4-loop-proof` was
    # counted as our own beacon — the slug list overruling a real verdict is
    # the bug the backstop is supposed to be a backstop against.
    member_verdict = origin_verdict_for_member(db, member)
    synthetic_ingested = 0

    for lr in loop_runs:
        slug = (lr.get("loop_slug") or "")[:255]
        run_synthetic = member_verdict if member_verdict is not None else slug in SELF_ORIGINATED_LOOP_SLUGS
        synthetic_ingested += int(run_synthetic)
        db.add(
            LoopRun(
                member_id=member.id,
                fleet_id=member.fleet_id,
                loop_slug=slug,
                is_synthetic=run_synthetic,
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

    # ── skill_errors ─ RETIRED (t_a7ebcc96, 2026-11-16) ─────────────────────
    # This block was the sole writer of skill_error_reports. It read
    # payload["skill_errors"], a key no client has ever produced; the table held
    # 0 rows across the feature's entire life while this endpoint ingested
    # 22,834 accepted sync cycles.

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
    # actual-state read surface)…
    member.updated_at = now
    # …and stamp the operational ping (t_5734546c): an accepted sync-report
    # is itself proof of life, so fleet_member_liveness.last_ping_at now
    # carries the same instant — the writer that was missing is the one the
    # collector already makes on every cycle. Same transaction, same commit.
    ping_result = _stamp_member_liveness(db, member, now)

    db.commit()

    # mesh_0408 W4: the ack reports a run count, so it reports the split too —
    # ``loop_runs`` alone would be the same combined-figure-only lie the
    # dashboard used to tell.
    ingested = RunCounts(total=len(loop_runs), synthetic=synthetic_ingested)
    # t_a7ebcc96: skill_errors is no longer persisted, but the ack still counts
    # what arrived, under a key that says so. The old ack reported
    # ``"skill_errors": N`` from a table that never held a row — an unfalsifiable
    # number that read as health. ``skill_errors_ignored`` makes the dead rail
    # observable instead: a client that starts sending the field sees its own
    # payload counted and dropped, rather than a silent success.
    _se_arrived = payload.get("skill_errors")
    recorded: dict[str, int | bool | str] = {
        "loop_runs": ingested.total,
        "loop_runs_synthetic": ingested.synthetic,
        "loop_runs_external": ingested.external,
        "skill_errors_ignored": len(_se_arrived) if isinstance(_se_arrived, list) else 0,
        "cron_health": cron_stored,
        "lockfile_state": lockfile_stored,
        # t_5734546c: derived from the actual write ("created" | "refreshed"),
        # never a constant — an ack key the write path cannot falsify is the
        # same lie class the old skill_errors count was.
        "member_ping": ping_result,
    }
    return recorded, truncated


# ── Retention / lifecycle API ──────────────────────────────────────────────
# Split into app/services/sync_report_retention.py (600-line module gate). The
# re-export is load-bearing: app/admin_routes.py, scripts and
# tests/test_activate0701_sync_report.py import these names from here, and
# test_activate0701 monkeypatches them as attributes OF THIS MODULE.
from app.services.sync_report_retention import (  # noqa: F401
    DEFAULT_RETENTION_DAYS,
    MAX_ROLLUP_SCAN_DAYS,
    days_with_raw_rows,
    prune_raw,
    raw_counts_before,
    raw_day_groups,
    retention_cutoff,
    rollup_loop_runs,
    uncovered_day_groups,
    utc_today,
)


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
    since = utc_today() - timedelta(days=days)
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
