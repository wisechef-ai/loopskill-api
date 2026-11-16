"""Raw-telemetry retention for the sync-report pipeline.

Split out of ``app/services/sync_report.py`` under the 600-line module gate
(tests/test_w0_2_pyfile_size_discipline.py) when t_28f34a8a wired the prune to
a schedule. Two halves of one domain, separated by what they touch:

  * ``sync_report.py``        — the WRITE path (ingest, caps, per-field truncation)
  * this module               — the LIFECYCLE path (day buckets, rollup, coverage
                                guard, prune, before/after counts)

``sync_report.py`` re-exports everything here so existing callers keep working;
new callers import from this module directly.

RETENTION IS SCHEDULED, NOT ASSUMED: raw rows are deleted only by
``app/crons/sync_report_pruner.py`` on the ``wisechef`` crontab. Before this was
wired the docstring here promised 30-day retention while prod carried 137 days
(t_28f34a8a) -- see ``deploy/crontab.sync-report-retention`` and
``~/.hermes/goals/sync-report-retention-30d.md``.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import CronHealthSnapshot, LoopRun, LoopRunDailyRollup

logger = logging.getLogger(__name__)

#: Retention window for raw telemetry rows (``loop_runs``,
#: ``cron_health_snapshots``). Rows older than this are deleted by the prune
#: cron; rollups are never deleted, so adoption history survives.
DEFAULT_RETENTION_DAYS = 30


def utc_today() -> date:
    """The current day on the rollup's clock.

    Rollup buckets are UTC days (``day_start`` below is midnight UTC), so the
    default day must be read on the SAME clock. ``date.today()`` is the host's
    local date: on a non-UTC host it names a different day for up to the UTC
    offset's worth of hours each night (00:00-02:00 under CEST), and a default
    rollup then aggregates a window that holds none of the day's runs.
    """
    return datetime.now(UTC).date()


# Upper bound on the day-by-day walk in :func:`days_with_raw_rows`. 400 days is
# ~13x the 30-day retention window: reaching it means the retention cron has been
# dead for over a year, which is a loud-failure condition, not a slow path.
MAX_ROLLUP_SCAN_DAYS = 400


def _as_utc(value: datetime) -> datetime:
    """Normalise a DB-returned timestamp to an aware UTC datetime.

    SQLite has no timezone type: ``DateTime(timezone=True)`` columns come back
    NAIVE (UTC, offset dropped by the dialect's storage format) while Postgres
    returns them AWARE. Any comparison across the two engines has to canonicalise
    first or the same code path means different instants per leg.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` UTC window covering one rollup day.

    ONE definition on purpose. ``rollup_loop_runs`` buckets raw rows with this
    predicate and the retention guard (:func:`uncovered_day_groups`) walks days
    with the SAME one. If the two ever disagreed about which UTC day a row
    belongs to, the guard could certify a day as covered while the rollup had
    aggregated a different window — and the prune that follows would delete rows
    whose aggregate was never written. ``utc_today()`` is the matching clock for
    the *default* day; see fix-mesh0408-host-dependence in app/version.py.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


def retention_cutoff(
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    reference_time: datetime | None = None,
) -> datetime:
    """Exclusive cutoff for raw retention, aligned to a UTC day boundary.

    Day-aligned rather than ``now - N days`` because a rollup day must be
    **all-or-nothing** for the prune. A wall-clock cutoff slices through the day
    that contains it, so that day keeps a surviving half; the next daily rollup
    re-aggregates it from the surviving half and silently shrinks a number that
    was already correct. At a day boundary a day is wholly present or wholly
    gone, and re-aggregating it always reads the complete population.

    ``reference_time`` is injectable so the cutoff is testable without freezing
    the clock process-wide (same shape as ``stripe_event_pruner.prune``).
    """
    ref = _as_utc(reference_time or datetime.now(UTC))
    return datetime.combine(ref - timedelta(days=older_than_days), datetime.min.time(), tzinfo=UTC)


def _day_groups(db: Session, day: date, *, before: datetime | None = None) -> list[tuple[Any, Any, str]]:
    """Distinct ``(fleet_id, member_id, loop_slug)`` for rows in ``day``.

    ``before`` is an optional EXCLUSIVE upper bound, applied inside the same
    query as the day window. The retention guard needs it: a day straddling the
    cutoff holds both doomed and retained rows, and without the bound it would be
    reported as needing coverage when nothing in it is actually being deleted.
    """
    day_start, day_end = _day_bounds(day)
    query = db.query(LoopRun.fleet_id, LoopRun.member_id, LoopRun.loop_slug).filter(
        LoopRun.created_at >= day_start, LoopRun.created_at < day_end
    )
    if before is not None:
        query = query.filter(LoopRun.created_at < _as_utc(before))
    # list[tuple] not list[Row]: Row is a tuple subclass and the annotation is
    # invariant, so returning the raw Result rows fails a strict type check while
    # being fine at runtime.
    return [
        tuple(row) for row in query.group_by(LoopRun.fleet_id, LoopRun.member_id, LoopRun.loop_slug).all()
    ]


def raw_day_groups(db: Session, day: date) -> list[tuple[Any, Any, str]]:
    """Distinct ``(fleet_id, member_id, loop_slug)`` holding raw rows in ``day``."""
    return _day_groups(db, day)


def days_with_raw_rows(
    db: Session,
    *,
    before: datetime | None = None,
    max_days: int = MAX_ROLLUP_SCAN_DAYS,
) -> list[date]:
    """Every UTC day that still holds at least one raw ``LoopRun`` row.

    Walks day by day instead of grouping on ``date(created_at)`` in SQL so that
    membership is decided by :func:`_day_bounds` — the identical predicate the
    rollup uses — and so no dialect-specific date function can bucket a row
    differently on SQLite vs Postgres. ``before`` is exclusive: a day counts only
    if it holds a row strictly older than it, which the per-day probe enforces in
    SQL rather than by day arithmetic, so a single-row table and a table whose
    rows all share one timestamp behave the same.

    Raises ``RuntimeError`` past ``max_days``: a history that long means
    retention has been dead for over a year and paging through it silently is
    how that stops being visible.
    """
    lo = db.query(func.min(LoopRun.created_at)).scalar()
    hi = db.query(func.max(LoopRun.created_at)).scalar()
    if lo is None or hi is None:
        return []
    lo = _as_utc(lo)
    upper = _as_utc(hi)
    if before is not None:
        upper = min(upper, _as_utc(before))

    days: list[date] = []
    cursor = lo.date()
    last = upper.date()
    while cursor <= last:
        if len(days) >= max_days:
            raise RuntimeError(
                f"days_with_raw_rows: hit max_days={max_days} scanning "
                f"{lo.date()}..{last} — raw loop_runs history is far past the "
                f"{DEFAULT_RETENTION_DAYS}d retention window; fix the cron, do not widen this bound"
            )
        if _day_groups(db, cursor, before=before):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def rollup_loop_runs(db: Session, day: date | None = None) -> int:
    """Aggregate all LoopRun rows for ``day`` into LoopRunDailyRollup.

    Idempotent *and non-destructive*, which the original was not. The shape is
    delete-then-insert, so it must only ever run while the raw rows it re-derives
    from are still present. Two guards make that true:

    * groups are computed **before** the delete, so a crash mid-way cannot leave a
      day with no rollup and no raw rows;
    * a day with **no raw rows** is never touched at all. Re-aggregating an already
      pruned day used to delete the rollup and insert nothing back — which, since
      rollups are the only copy of adoption numbers older than the retention
      window, is permanent data loss reachable from a single admin POST
      (``POST /api/admin/loop-run-rollup {"day": "2026-07-02"}``).

    Returns the number of rollup rows written (0 also means "left alone").
    """
    if day is None:
        day = utc_today()

    day_start, day_end = _day_bounds(day)

    # Aggregate from raw rows — load grouped rows, then iterate to compute
    # all aggregates per (fleet, member, loop_slug). Computed BEFORE the delete.
    groups = raw_day_groups(db, day)

    if not groups:
        existing = (
            db.query(func.count())
            .select_from(LoopRunDailyRollup)
            .filter(LoopRunDailyRollup.day == day)
            .scalar()
            or 0
        )
        if existing:
            logger.warning(
                "sync-report rollup: day %s has 0 raw loop_runs but %d rollup row(s) — refusing to "
                "re-aggregate. The raw rows are already pruned and the rollup is the only surviving "
                "copy; delete-then-insert here would destroy the numbers, not refresh them.",
                day,
                existing,
            )
        return 0

    # Delete existing rollups for this day so re-insertion is idempotent.
    db.query(LoopRunDailyRollup).filter(LoopRunDailyRollup.day == day).delete()

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
        # mesh_0408 W4: carried onto the rollup because raw rows are pruned at
        # 30d — without it, every adoption number older than a month would
        # silently re-merge our own beacon back into the total.
        synthetic = sum(1 for r in group_rows if r.is_synthetic)
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
                synthetic_runs=synthetic,
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


def uncovered_day_groups(
    db: Session,
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    reference_time: datetime | None = None,
    max_days: int = MAX_ROLLUP_SCAN_DAYS,
) -> list[tuple[Any, Any, str, date]]:
    """Raw ``(fleet_id, member_id, loop_slug, day)`` groups with **no** rollup row.

    This is the prune's precondition. A group is uncovered when raw rows exist for
    it but no ``LoopRunDailyRollup`` row does — i.e. the only copy of that day's
    adoption numbers is the set of rows about to be deleted. Deleting those is not
    retention, it is destruction: the 30d contract assumes the rollup happened
    first, and the rollup is exactly the thing that had never run in prod
    (t_28f34a8a found 46 raw day-groups against 2 rollup rows).

    Reads rollups, never writes them — same rule as :func:`prune_raw`.
    """
    cutoff = retention_cutoff(older_than_days, reference_time)
    uncovered: list[tuple[Any, Any, str, date]] = []
    for day in days_with_raw_rows(db, before=cutoff, max_days=max_days):
        covered = {
            tuple(row)
            for row in db.query(
                LoopRunDailyRollup.fleet_id,
                LoopRunDailyRollup.member_id,
                LoopRunDailyRollup.loop_slug,
            )
            .filter(LoopRunDailyRollup.day == day)
            .all()
        }
        uncovered.extend((*group, day) for group in raw_day_groups(db, day) if group not in covered)
    return uncovered


def prune_raw(
    db: Session,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    reference_time: datetime | None = None,
) -> dict[str, int]:
    """Delete LoopRun + CronHealthSnapshot rows past retention.

    NEVER touches rollups. Returns counts of deleted rows per table.

    MECHANISM ONLY — it does not check that a rollup exists for what it deletes,
    because a pure delete has to stay independently testable. No production
    caller may call it directly: go through
    :func:`app.crons.sync_report_pruner.prune`, which rolls up first and refuses
    to delete uncovered days. ``test_prune_raw_has_no_ungoverned_caller`` fails
    if a bare call appears anywhere else in ``app/``.
    """
    cutoff = retention_cutoff(older_than_days, reference_time)

    loop_run_count = db.query(LoopRun).filter(LoopRun.created_at < cutoff).delete()
    cron_count = db.query(CronHealthSnapshot).filter(CronHealthSnapshot.created_at < cutoff).delete()
    db.commit()

    logger.info(
        "sync-report prune: deleted %d loop_runs, %d cron_health_snapshots (older than %d days, cutoff %s)",
        loop_run_count,
        cron_count,
        older_than_days,
        cutoff.date(),
    )
    return {"loop_runs": loop_run_count, "cron_health_snapshots": cron_count}


def raw_counts_before(
    db: Session,
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    reference_time: datetime | None = None,
) -> dict[str, int]:
    """How many raw rows a prune at this cutoff WOULD delete. Read-only preview."""
    cutoff = retention_cutoff(older_than_days, reference_time)
    return {
        "loop_runs": db.query(func.count()).select_from(LoopRun).filter(LoopRun.created_at < cutoff).scalar()
        or 0,
        "cron_health_snapshots": db.query(func.count())
        .select_from(CronHealthSnapshot)
        .filter(CronHealthSnapshot.created_at < cutoff)
        .scalar()
        or 0,
    }
