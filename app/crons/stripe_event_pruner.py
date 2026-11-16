"""Stripe webhook event-id TTL pruner — t_ae1bddaa, wired by t_f1867996.

stripe_event_ids is the webhook idempotency table (WIS-569): one row per
processed Stripe event.id, inserted by
app/subscription_service.record_event_or_skip(). Stripe's documented
redelivery window is ~3 days
(https://docs.stripe.com/webhooks — "Stripe tries again over the course of
three days"); a 7-day TTL comfortably exceeds it, so pruning rows older than
7 days can never re-open a replay window. Idempotent.

WHY CRONTAB, AND WHY THE SHIPPED SYSTEMD UNIT IS GONE
-----------------------------------------------------
This module shipped with two systemd unit files under ``deploy/``
and a docstring citing them as its schedule. Both were fiction. On
wisechef-hq — the box that serves prod — ``systemctl list-timers --all``
listed **zero** loopskill/recipes units, and the unit started
``/opt/recipes-api/.venv/bin/python``, a prefix that does not exist there
(the repo lives at ``/home/wisechef/loopskill-api`` and is deployed by the
self-hosted runner's pull+restart). The outcome proof that it never ran:
measured 2026-11-16, ``stripe_event_ids`` held 15 rows of which **13 were
older than the 7-day TTL, the oldest from 2026-07-11** — four months of webhook
idempotency keys against a 7-day window, with no log line from this module
anywhere in ``/home/wisechef/wiserecipes-logs/`` and no journal entry for the
unit. An uninstalled unit file is not documentation, it is a false attestation
of a schedule, and it is what let this pruner look wired for months; the units
are deleted in this commit.

The live mechanism on this host is the ``wisechef`` **user crontab**, so the
line below is the schedule of record. It is mirrored byte-for-byte into
``deploy/crontab.stripe-event-pruner`` and asserted equal by
``tests/test_crons_wiring.py``, so code and schedule cannot drift.

THE FLOOR IS A GUARD, NOT A COMMENT
-----------------------------------
">= 7 days" is a Stripe-replay-safety constraint, not style. It is enforced in
:func:`cutoff` (raises) and in :func:`main` (exit **2**), so a runbook cannot
"clean up" the table with ``--days 0`` and silently re-open replay for every
event Stripe is still redelivering.

Manual runs (idempotent, safe to repeat):

    cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a
    venv/bin/python -m app.crons.stripe_event_pruner --dry-run   # counts only
    venv/bin/python -m app.crons.stripe_event_pruner             # deletes

Exit codes: 0 = pruned (or nothing to do), 2 = REFUSED on the retention floor,
1 = unexpected error. 2 is deliberately distinct from 1 so a log grep can tell
"someone asked for an unsafe window" apart from "the code crashed".
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.crons._cli import Parser as CliParser, positive_int
from app.models import StripeEventId

logger = logging.getLogger("loopskill.stripe_event_pruner")

# >= 7 days retention is the task spec floor; 7d is ~2.3x Stripe's ~3-day
# redelivery window, so pruning can never re-open a replay.
TTL_DAYS = 7

# The floor is not tunable downward. Anything below it re-opens the window
# Stripe may still redeliver into, which turns the idempotency table into a
# replay hole. Enforced, not merely documented — see the module docstring.
MIN_TTL_DAYS = 7

# The schedule of record — single source of truth, mirrored byte-for-byte into
# deploy/crontab.stripe-event-pruner and asserted equal by
# tests/test_crons_wiring.py so the two cannot drift.
# 03:50 UTC: after the 03:40 sync-report retention prune, clear of the 03:00
# federation reindex / 03:15 connector walk / 03:20 skill-edge build, and
# before the 05:00 backups. ONE `flock -n -E 4`, not a probe-and-run pair: the
# reserved exit 4 and the guarded skip marker are explained in app/crons/_cli.py.
# flock -n means an over-long run cannot overlap the next night's run.
CRONTAB_LINE = (
    "50 3 * * * cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a; "
    "flock -n -E 4 /tmp/loopskill-stripe-event-pruner.lock venv/bin/python -m app.crons.stripe_event_pruner >> /home/wisechef/wiserecipes-logs/stripe_event_pruner.log 2>&1; "
    'rc=$?; [ $rc -ne 4 ] || echo \'{"skipped": "lock held by a live run"}\' >> /home/wisechef/wiserecipes-logs/stripe_event_pruner.log 2>&1; '
    "exit $rc"
)


class RetentionFloorError(ValueError):
    """Requested retention is below the Stripe-replay-safety floor."""


def cutoff(
    reference_time: datetime | None = None,
    ttl_days: int = TTL_DAYS,
) -> datetime:
    """The instant a row must be strictly older than to be prunable.

    Raises :class:`RetentionFloorError` below ``MIN_TTL_DAYS``: every caller
    (CLI, tests, a future admin route) gets the same floor, because the floor
    is a property of the data, not of the entry point.

    Checked TWICE, because either half can be passed on its own:

    * the requested ``ttl_days`` — ``--days 3`` must be refused outright;
    * the *effective* retention, ``now() - cutoff``, measured against the real
      wall clock. Without this, a caller supplying ``reference_time`` (a
      backfill, an admin route, a copy-pasted test) clears the ``ttl_days``
      check with a reference in 2100 and deletes the whole idempotency table —
      exactly the replay window the floor exists to protect.
    """
    if ttl_days < MIN_TTL_DAYS:
        raise RetentionFloorError(
            f"refusing to prune stripe_event_ids with ttl_days={ttl_days}: the floor is "
            f"{MIN_TTL_DAYS}d (Stripe redelivers for ~3 days; a shorter window re-opens "
            "webhook replay)"
        )
    now = datetime.now(UTC)
    ref = reference_time if reference_time is not None else now
    cutoff_at = ref - timedelta(days=ttl_days)
    effective_days = (now - cutoff_at).total_seconds() / 86400.0
    if effective_days < MIN_TTL_DAYS:
        raise RetentionFloorError(
            f"refusing to prune stripe_event_ids at {cutoff_at:%Y-%m-%dT%H:%M:%SZ}: effective "
            f"retention would be {effective_days:.2f}d, below the {MIN_TTL_DAYS}d floor "
            f"(ttl_days={ttl_days}, reference_time={reference_time!r}). The floor is measured "
            "against the wall clock, not only against the requested window."
        )
    return cutoff_at


def rows_past_ttl(db: Session, cutoff_at: datetime) -> int:
    """Count rows :func:`prune` would delete. Read-only — the dry-run path."""
    return db.query(StripeEventId).filter(StripeEventId.processed_at < cutoff_at).count()


def prune(
    reference_time: datetime | None = None,
    db: Session | None = None,
    ttl_days: int = TTL_DAYS,
) -> int:
    """Delete stripe_event_ids rows older than ``ttl_days``. Returns rows removed.

    Pass `db` to run against an existing session (tests); opens its own
    SessionLocal otherwise.
    """
    cutoff_at = cutoff(reference_time=reference_time, ttl_days=ttl_days)
    owns_session = db is None
    if owns_session:
        # Late import on purpose (same trick as crons/sync_report_pruner.py): the
        # name is resolved at CALL time, so a test that monkeypatches
        # app.database.SessionLocal lands on this call instead of being shadowed
        # by a module-level binding made at import time.
        from app.database import SessionLocal

        session = SessionLocal()
    else:
        session = db
    try:
        deleted = (
            session.query(StripeEventId)
            .filter(StripeEventId.processed_at < cutoff_at)
            .delete(synchronize_session=False)
        )
        session.commit()
        logger.info("stripe_event_pruner: removed %d rows older than %s", deleted, cutoff_at)
        return deleted
    finally:
        if owns_session:
            session.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — this is what cron runs, so it is what the tests pin."""
    ap = CliParser(
        prog="stripe_event_pruner",
        description=f"TTL prune of stripe_event_ids (default {TTL_DAYS} days).",
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
    # The reference instant is captured ONCE and threaded into both the report
    # and the delete. Computing it twice (once for the JSON line, once inside
    # prune()) means a run that straddles midnight deletes one day MORE than
    # it reports, and the JSON line the retention predicate greps would name a
    # cutoff the DELETE never used — unreconstructable in a post-mortem.
    reference = datetime.now(UTC)
    report: dict[str, Any] = {"table": "stripe_event_ids", "days": days, "dry_run": args.dry_run}
    try:
        cutoff_at = cutoff(reference_time=reference, ttl_days=days)
    except RetentionFloorError as exc:
        logger.error("%s", exc)
        print(json.dumps({**report, "blocked": True, "error": str(exc)}, sort_keys=True))
        return 2

    from app.database import SessionLocal  # late import — see prune() for why

    report["cutoff"] = cutoff_at.isoformat()
    db = SessionLocal()
    try:
        if args.dry_run:
            report["would_delete"] = rows_past_ttl(db, cutoff_at)
            report["deleted"] = 0
            print(json.dumps(report, sort_keys=True))
            return 0
        report["deleted"] = prune(reference_time=reference, db=db, ttl_days=days)
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 — Rationale: top-level cron; log-and-exit-nonzero
        logger.exception("stripe_event_pruner: unhandled error")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
