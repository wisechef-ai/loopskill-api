"""Phase D — 90-day TTL pruner for fleet_pings, wired by t_f1867996.

Drops rows whose ``last_seen_day`` is more than 90 days behind today.
Idempotent.

``fleet_pings`` is the anonymised fleet heartbeat surface (Phase D, F8 fix):
keyed ``blake2b(salt)`` hash + day, nothing else, so it is aggregate liveness
only — see ``app/services/drift_observability.py`` for the reader. 90 days is a
**product floor**: it is the window the liveness/rollup metrics are allowed to
see, so the pruner may not be tuned downward to make a row count look better.

WHY CRONTAB, AND WHY NO SYSTEMD UNIT IS CITED ANY MORE
------------------------------------------------------
This docstring used to say "Run weekly. Schedule: see ``deploy/cron.d`` (or
systemd timer)". Neither the cron.d file nor the units behind that claim existed. On wisechef-hq — the box that serves prod —
``systemctl list-timers --all`` listed **zero** loopskill/recipes units, and the
only unit files in ``deploy/`` started
``/opt/recipes-api/.venv/bin/python`` and ``/srv/recipes-api/.venv/bin/python``,
prefixes that do not exist on that host. The outcome proof: measured
2026-11-16, ``fleet_pings`` held 139 rows of which **47 were older than the
90-day TTL, the oldest ``last_seen_day`` being 2026-07-02** — the pruner had
never run, and no log line from this module existed anywhere in
``/home/wisechef/wiserecipes-logs/``.

The live mechanism on this host is the ``wisechef`` **user crontab**, so the
line below is the schedule of record: mirrored byte-for-byte into
``deploy/crontab.fleet-ping-pruner`` and asserted equal by
``tests/test_crons_wiring.py``.

Cadence is **daily**, not weekly as the old docstring claimed: the TTL is 90
days, so an extra run costs one 0-row ``DELETE``, and in exchange the job's own
log line becomes a daily liveness signal the standing predicate
(``~/.hermes/goals/fleet-ping-retention-90d.md``) can assert on. A weekly job
that silently stopped firing is indistinguishable from a working one for six
days; a daily one is not.

Manual runs (idempotent, safe to repeat):

    cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a
    venv/bin/python -m app.crons.fleet_ping_pruner --dry-run   # counts only
    venv/bin/python -m app.crons.fleet_ping_pruner             # deletes

Exit codes: 0 = pruned (or nothing to do), 2 = REFUSED on the retention floor,
1 = unexpected error.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.crons._cli import Parser as CliParser, positive_int
from app.models import FleetPing

logger = logging.getLogger("loopskill.fleet_ping_pruner")

TTL_DAYS = 90

# Product floor: 90d is the window the anonymised fleet-liveness metrics are
# allowed to see. Shortening it does not "clean up" anything, it deletes
# customer-visible aggregate history out from under the dashboards.
MIN_TTL_DAYS = 90

# The schedule of record — mirrored byte-for-byte into
# deploy/crontab.fleet-ping-pruner and asserted equal by tests/test_crons_wiring.py.
# 03:55 UTC: last of the 03:xx retention lane (03:40 sync-report, 03:50 stripe),
# clear of the 03:00/03:15/03:20 jobs and before the 05:00 backups. ONE
# `flock -n -E 4`, not a probe-and-run pair: the reserved exit 4 and the guarded
# skip marker are explained in app/crons/_cli.py. flock -n so an over-long run can
# never overlap the next night's run.
CRONTAB_LINE = (
    "55 3 * * * cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a; "
    "flock -n -E 4 /tmp/loopskill-fleet-ping-pruner.lock venv/bin/python -m app.crons.fleet_ping_pruner >> /home/wisechef/wiserecipes-logs/fleet_ping_pruner.log 2>&1; "
    'rc=$?; [ $rc -ne 4 ] || echo \'{"skipped": "lock held by a live run"}\' >> /home/wisechef/wiserecipes-logs/fleet_ping_pruner.log 2>&1; '
    "exit $rc"
)


class RetentionFloorError(ValueError):
    """Requested retention is below the fleet-liveness product floor."""


def utc_today() -> date:
    """Today on the SAME clock as the cron schedule and the reader.

    ``date.today()`` is the host's LOCAL date. The crontab line is UTC,
    ``stripe_event_pruner`` uses ``datetime.now(UTC)``, and the consumer
    (``app.services.drift_observability.fleet_liveness``) buckets on
    ``date.today()`` of the *server* too — but this job's floor is defined in
    UTC days, so on a UTC+X host a manual run between 00:00 local and 00:00
    UTC would compute a reference day one ahead of the UTC day and delete rows
    that are still inside the 90-day window. Same defect class
    ``app.services.sync_report_retention.utc_today()`` exists for.
    """
    return datetime.now(UTC).date()


def cutoff(
    reference_day: date | None = None,
    ttl_days: int = TTL_DAYS,
) -> date:
    """The day a ``last_seen_day`` must be strictly earlier than to be prunable.

    Raises :class:`RetentionFloorError` below ``MIN_TTL_DAYS`` — the floor holds
    for every caller, not just the CLI.

    Checked TWICE: the requested ``ttl_days``, and the *effective* retention
    (``today - cutoff``) against the real UTC clock. Without the second check a
    caller passing ``reference_day`` (backfill, admin route, copy-pasted test)
    clears the ``ttl_days`` gate with a day in 2100 and empties the table —
    including the rows inside the 90-day window the floor protects.
    """
    if ttl_days < MIN_TTL_DAYS:
        raise RetentionFloorError(
            f"refusing to prune fleet_pings with ttl_days={ttl_days}: the floor is "
            f"{MIN_TTL_DAYS}d (90d is the aggregate-liveness window the product reads; "
            "a shorter window deletes customer-visible history)"
        )
    today = utc_today()
    ref = reference_day if reference_day is not None else today
    cutoff_day = ref - timedelta(days=ttl_days)
    if cutoff_day > today - timedelta(days=MIN_TTL_DAYS):
        raise RetentionFloorError(
            f"refusing to prune fleet_pings at {cutoff_day}: effective retention would be "
            f"{(today - cutoff_day).days}d, below the {MIN_TTL_DAYS}d floor "
            f"(ttl_days={ttl_days}, reference_day={reference_day!r}). The floor is measured "
            "against the UTC wall clock, not only against the requested window."
        )
    return cutoff_day


def _stale_condition(reference_day: date, cutoff_day: date):
    """The rows the retention guarantee cannot keep.

    Two populations, not one:

    * ``last_seen_day < cutoff_day`` — past the 90-day window;
    * ``last_seen_day > reference_day`` — dated in the FUTURE.

    The second is not hypothetical. ``last_seen_day`` is client-supplied and
    unvalidated (``HeartbeatPayload.last_seen_day: date`` in
    ``app/heartbeat_routes.py`` has no clamp), and ``POST /api/v1/heartbeat`` is
    an unauthenticated public path. A skewed agent clock — or anyone who wants
    a row that outlives every prune — can insert ``2099-01-01`` today, and a
    one-sided ``<`` predicate means it survives the pruner forever, which
    contradicts this job being the table's retention guarantee. It is not
    inert either: ``fleet_liveness`` counts any row with
    ``last_seen_day >= window_start`` as a live agent, so a future-dated row
    inflates the metric too. Deleting them is the safe direction (the data is
    invalid on arrival); the write-side clamp belongs in the heartbeat schema.
    """
    return or_(
        FleetPing.last_seen_day < cutoff_day,
        FleetPing.last_seen_day > reference_day,
    )


def rows_past_ttl(db: Session, cutoff_day: date, reference_day: date | None = None) -> int:
    """Count rows :func:`prune` would delete. Read-only — the dry-run path.

    Uses the SAME condition as ``prune()``; if the two ever disagree, the
    ``--dry-run`` pre-flight stops predicting the delete it is meant to predict.
    """
    ref = reference_day if reference_day is not None else utc_today()
    return db.query(FleetPing).filter(_stale_condition(ref, cutoff_day)).count()


def prune(
    reference_day: date | None = None,
    ttl_days: int = TTL_DAYS,
    db: Session | None = None,
) -> int:
    """Delete rows older than ``ttl_days``. Returns number of rows removed.

    Pass ``db`` to run against an existing session (tests); opens its own
    ``SessionLocal`` otherwise.
    """
    ref = reference_day if reference_day is not None else utc_today()
    cutoff_day = cutoff(reference_day=ref, ttl_days=ttl_days)
    owns_session = db is None
    if owns_session:
        # Late import so a monkeypatched app.database.SessionLocal lands here.
        from app.database import SessionLocal

        session = SessionLocal()
    else:
        session = db
    try:
        deleted = (
            session.query(FleetPing)
            .filter(_stale_condition(ref, cutoff_day))
            .delete(synchronize_session=False)
        )
        session.commit()
        logger.info(
            "fleet_ping_pruner: removed %d rows older than %s (reference %s)",
            deleted,
            cutoff_day,
            ref,
        )
        return deleted
    finally:
        if owns_session:
            session.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — this is what cron runs, so it is what the tests pin."""
    ap = CliParser(
        prog="fleet_ping_pruner",
        description=f"TTL prune of fleet_pings (default {TTL_DAYS} days).",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=TTL_DAYS,
        help=f"retention window in days (default {TTL_DAYS}, floor {MIN_TTL_DAYS})",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report the rows past the window; delete nothing",
    )
    args = ap.parse_args(argv)
    days = positive_int("days", args.days)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Captured ONCE and threaded into the report AND the delete, so the cutoff
    # in the JSON line is by construction the cutoff the DELETE used. Computed
    # twice from the wall clock, a run straddling midnight deletes one day more
    # than it reports and the logged audit line names a cutoff that never ran.
    reference = utc_today()
    report: dict[str, Any] = {"table": "fleet_pings", "days": days, "dry_run": args.dry_run}
    try:
        cutoff_day = cutoff(reference_day=reference, ttl_days=days)
    except RetentionFloorError as exc:
        logger.error("%s", exc)
        print(json.dumps({**report, "blocked": True, "error": str(exc)}, sort_keys=True))
        return 2

    from app.database import SessionLocal  # late import — see prune() for why

    report["cutoff"] = cutoff_day.isoformat()
    db = SessionLocal()
    try:
        if args.dry_run:
            report["would_delete"] = rows_past_ttl(db, cutoff_day, reference)
            report["deleted"] = 0
            print(json.dumps(report, sort_keys=True))
            return 0
        report["deleted"] = prune(reference_day=reference, ttl_days=days, db=db)
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 — Rationale: top-level cron; log-and-exit-nonzero
        logger.exception("fleet_ping_pruner: unhandled error")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
