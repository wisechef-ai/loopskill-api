"""Raw sync-report telemetry retention cron — t_28f34a8a.

Closes the gap where ``prune_raw`` was written, tested and documented since
activate_0701 Phase T but had **no caller**: prod carried 137 days of raw
``loop_runs`` + ``cron_health_snapshots`` against a documented 30-day window
(measured 2026-11-16: loop_runs 20,829 rows oldest 2026-07-02 17:35,
cron_health_snapshots 22,861 rows oldest 2026-07-02 17:34).

WHY THIS ORDER — rollup, then guard, then delete
------------------------------------------------
The rollup is not an optional pre-step; it is the only thing that makes the
delete be retention rather than destruction. ``loop_run_daily_rollups`` is the
sole durable copy of adoption numbers older than the window (that is literally
why ``synthetic_runs`` is denormalised onto it), and at audit time it held **2
rows for 46 raw day-groups**. Had the prune simply been wired the obvious way,
its first run would have deleted all 20,829 raw loop_runs rows — every beacon
day from 2026-07-02 to 2026-09-16 — with no aggregate left behind. So:

1. ``rollup_all_days_with_raw_rows`` re-aggregates every day that still has raw
   rows (bounded, self-healing: a missed night is picked up the next).
2. ``uncovered_day_groups`` re-checks coverage for exactly the window about to
   be deleted. Any gap → **refuse**, exit 2. Fail-closed: the rollup step could
   have died between 1 and 2 (OOM, lock, deploy restart) and the numbers only
   survive because step 2 does not trust step 1's success.
3. Only then ``prune_raw``, logging per-table delete counts.

WHY CRONTAB AND NOT A SYSTEMD TIMER
----------------------------------
Every other pruner in ``app/crons/`` ships ``deploy/*.timer`` + ``*.service``
and cites them as its schedule. That path is fiction on the box that actually
serves prod: ``systemctl list-timers`` on wisechef-hq returns zero
loopskill/recipes units (measured 2026-11-16), the units point at a
``/opt/recipes-api`` prefix that no longer exists, and the two surviving
pruners are provably dead — ``stripe_event_ids`` holds 13 rows older than its
7d TTL and ``fleet_pings`` holds 47 rows older than its 90d TTL. A schedule that
is never installed is indistinguishable from no schedule, which is this card's
title. The live mechanism on this host is the ``wisechef`` user crontab (12
working entries, same ``cd + . ./.env + venv/bin/python >> log`` shape), so the
line below is the schedule of record. Install it, do not re-document it:

    (crontab -l; echo "$(python -c 'from app.crons.sync_report_pruner import CRONTAB_LINE; print(CRONTAB_LINE)')") | crontab -

Manual runs (idempotent, safe to repeat):

    cd ~/loopskill-api && set -a; . ./.env; set +a
    venv/bin/python -m app.crons.sync_report_pruner --dry-run   # preview, writes nothing
    venv/bin/python -m app.crons.sync_report_pruner             # roll up + prune

Exit codes: 0 = pruned (or nothing to do), 2 = BLOCKED on missing rollup
coverage, 1 = unexpected error. 2 is deliberately distinct from 1 so the cron
mail / log grep can tell "refused to destroy data" apart from "crashed".
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.sync_report_retention import (
    DEFAULT_RETENTION_DAYS,
    MAX_ROLLUP_SCAN_DAYS,
    days_with_raw_rows,
    prune_raw,
    raw_counts_before,
    rollup_loop_runs,
    uncovered_day_groups,
)

logger = logging.getLogger("loopskill.sync_report_pruner")

# The schedule of record — single source of truth, mirrored byte-for-byte into
# deploy/crontab.sync-report-retention and asserted equal by
# tests/test_sync_report_retention_cron.py so the two cannot drift.
# 03:40 UTC: clear of the 03:00 federation reindex, the 03:15 connector walk and
# the 03:20 skill-edge build; before the 05:00 backups. flock -n means an
# over-long run can never overlap the next night's run.
CRONTAB_LINE = (
    "40 3 * * * cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a; "
    "flock -n /tmp/loopskill-sync-report-retention.lock "
    "venv/bin/python -m app.crons.sync_report_pruner "
    ">> /home/wisechef/wiserecipes-logs/sync_report_retention.log 2>&1"
)

# Sample size for blocked-run logging: enough to act on, small enough that the
# line cannot blow up a log shipper.
SAMPLE_LIMIT = 5


def rollup_all_days_with_raw_rows(
    db: Session,
    *,
    max_days: int = MAX_ROLLUP_SCAN_DAYS,
) -> dict[str, int]:
    """Aggregate every UTC day that still holds raw rows. Returns coverage stats.

    Every day, not just today: the daily rollup is what makes the prune safe, and
    a rollup that only did "today" would leave any day written before the cron
    existed permanently uncovered — which is precisely how prod got 2 rollup rows
    for 46 day-groups. Idempotent, so re-running is free.
    """
    days = days_with_raw_rows(db, max_days=max_days)
    written = 0
    for day in days:
        written += rollup_loop_runs(db, day=day)
    logger.info("sync-report rollup: %d day(s) scanned, %d rollup row(s) written", len(days), written)
    return {"days": len(days), "rollup_rows": written}


def prune(
    reference_time: datetime | None = None,
    db: Session | None = None,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    *,
    allow_unrolled: bool = False,
) -> dict[str, Any]:
    """Roll up, verify coverage, then prune raw telemetry past retention.

    Returns ``{"blocked": bool, "uncovered_day_groups": int, "sample": [...],
    "rollup": {...}|None, "pruned": {...}|None}``. Idempotent: a second run
    reports ``pruned`` counts of 0. Pass ``db`` to run on an existing session
    (tests, the admin route); opens its own ``SessionLocal`` otherwise — same
    shape as ``stripe_event_pruner.prune``.

    ``allow_unrolled`` is the shell-side escape hatch. It exists so the answer to
    "I need this deleted now" is a logged flag rather than hand-written SQL, and
    it is deliberately NOT exposed over HTTP: anyone who can pass it already has
    shell + DB credentials.
    """
    owns_session = db is None
    if owns_session:
        # Late import on purpose (same trick as crons/drain_last_used.py): the
        # name is resolved at CALL time, so a test that monkeypatches
        # app.database.SessionLocal lands on this call instead of being shadowed
        # by a module-level binding made at import time.
        from app.database import SessionLocal

        session = SessionLocal()
    else:
        session = db
    try:
        rollup = rollup_all_days_with_raw_rows(session)
        uncovered = uncovered_day_groups(
            session,
            older_than_days=older_than_days,
            reference_time=reference_time,
        )

        if uncovered and not allow_unrolled:
            sample = [f"{g[2]}@{g[3]}" for g in uncovered[:SAMPLE_LIMIT]]
            logger.error(
                "sync-report prune BLOCKED: %d raw day-group(s) have no rollup row (e.g. %s). "
                "Refusing to delete — the rollup is the only surviving copy of those numbers. "
                "Re-run the rollup (python -m app.crons.sync_report_pruner) or pass --allow-unrolled "
                "if destroying them is intended.",
                len(uncovered),
                ", ".join(sample),
            )
            return {
                "blocked": True,
                "uncovered_day_groups": len(uncovered),
                "sample": sample,
                "rollup": rollup,
                "pruned": None,
            }

        if uncovered and allow_unrolled:
            logger.warning(
                "sync-report prune: --allow-unrolled set; deleting %d row group(s) that have NO rollup",
                len(uncovered),
            )

        counts = prune_raw(session, older_than_days=older_than_days, reference_time=reference_time)
        logger.info(
            "sync-report prune OK: loop_runs=%d cron_health_snapshots=%d retention_days=%d rollup=%s",
            counts["loop_runs"],
            counts["cron_health_snapshots"],
            older_than_days,
            rollup,
        )
        return {
            "blocked": False,
            "uncovered_day_groups": len(uncovered),
            "sample": [],
            "rollup": rollup,
            "pruned": counts,
        }
    finally:
        if owns_session:
            session.close()


def preview(
    db: Session,
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    """Read-only report of what :func:`prune` would do right now. Writes nothing.

    Used for the first contact with prod: the card requires the counts that are
    about to be destroyed to be snapshotted *before* the delete, and a dry run is
    also the only way to see the block decision without touching data.
    """
    counts = raw_counts_before(
        db,
        older_than_days=older_than_days,
        reference_time=reference_time,
    )
    uncovered = uncovered_day_groups(
        db,
        older_than_days=older_than_days,
        reference_time=reference_time,
    )
    return {
        "blocked": bool(uncovered),
        "would_delete": counts,
        "uncovered_day_groups": len(uncovered),
        "sample": [f"{g[2]}@{g[3]}" for g in uncovered[:SAMPLE_LIMIT]],
        "days_with_raw_rows": [str(d) for d in days_with_raw_rows(db)],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — this is what cron runs, so it is what the tests pin."""
    ap = argparse.ArgumentParser(
        prog="sync_report_pruner",
        description="30-day retention for raw sync-report telemetry (roll up, then prune).",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"retention window in days (default {DEFAULT_RETENTION_DAYS})",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be deleted and whether it is blocked; write nothing",
    )
    ap.add_argument(
        "--allow-unrolled",
        action="store_true",
        help="delete day-groups that have no rollup row (DESTROYS the only copy — logged)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from app.database import SessionLocal  # late import — see prune() for why

    db = SessionLocal()
    try:
        if args.dry_run:
            report = preview(db, older_than_days=args.days)
            print(json.dumps(report, default=str, sort_keys=True))
            if report["blocked"]:
                logger.error("dry-run: BLOCKED — %d group(s) lack a rollup", report["uncovered_day_groups"])
                return 2
            logger.info(
                "dry-run: would delete %d loop_runs, %d cron_health_snapshots",
                report["would_delete"]["loop_runs"],
                report["would_delete"]["cron_health_snapshots"],
            )
            return 0

        result = prune(db=db, older_than_days=args.days, allow_unrolled=args.allow_unrolled)
        print(json.dumps(result, default=str, sort_keys=True))
        return 2 if result["blocked"] else 0
    except Exception:  # noqa: BLE001 — Rationale: top-level cron; log-and-exit-nonzero
        logger.exception("sync_report_pruner: unhandled error")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
