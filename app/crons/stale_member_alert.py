"""Ops sweep for dark fleet hosts — t_e5dcc325.

Closes the gap where ``find_stale_members`` — the documented Phase-A
"failover replacement" whose operator outcome is *"a host went dark, do
something"* — had **zero callers in ``app/``**. Only tests imported it, so the
alert could never reach an operator while the module still claimed the trust
ledger was watching it. Nothing was watching it. Measured at audit time
(2026-11-27, prod ``loopskill`` on wisechef-hq): ``astrovita`` has been
``is_active=true`` since 2026-08-05 with **no liveness row at all** — 114 days
of a dark host that a correct, tested, never-invoked detector was silently
failing to report. Same class as ``prune_raw`` (4½ months with no caller) and
the ``skill_errors`` retirement.

Push, not just pull
-------------------
The fleet dashboard also surfaces this (``app/dashboard_routes.py``), but a
dashboard only tells you what someone thought to look at: ``astrovita`` sat dark
for 114 days precisely because nobody opened the page. A dark host is a
condition that must be *noticed on a schedule*, so this cron is the caller the
doctrine's "the trust ledger watches this" line was always promising.

Exit codes are part of the interface
------------------------------------
``app/crons/_cli.py`` owns the package-wide contract — ``0`` ran, ``1`` crashed,
``2`` refused, ``3`` usage error, ``4`` RESERVED for the flock conflict. This job
adds ONE code above that range rather than overloading a meaning it does not own:

* ``0`` — sweep ran, saw at least one active member, nothing dark.
* ``1`` — the sweeper itself failed.
* ``2`` — refused: asked to sweep a ``--fleet-id`` that matches no active member
          (a typo, or a deleted fleet). "I could not see anything" is not an
          all-clear and must not exit 0.
* ``3`` — usage error, via ``app.crons._cli.Parser`` (a typo'd flag must not
          look like a refusal, and still prints a JSON line on stdout).
* ``4`` — RESERVED by the package for the flock conflict; produced only by the
          ``-E 4`` wrapper in ``CRONTAB_LINE`` (never by this module — the
          wiring suite proves adversarial argv never returns 4), with a
          ``{"skipped": ...}`` marker so a locked-out pass leaves a trace.
* ``5`` — sweep ran and **found dark hosts**. Deliberately NOT ``3`` (which is
          usage) and NOT ``1`` (which is a crash): "the fleet has a problem"
          must never be confusable with "the sweeper crashed", or a crash reads
          as an all-clear and the alert dies the way the last one did — quietly.

WHY CRONTAB AND NOT A SYSTEMD TIMER
----------------------------------
Same reasoning as ``app/crons/sync_report_pruner.py``: the ``deploy/*.timer``
units other crons cite are not installed on the box that serves prod
(``systemctl list-timers`` lists zero loopskill units), so a timer file is
decoration. The live mechanism is the ``wisechef`` user crontab. The line below
is the schedule of record, mirrored byte-for-byte into
``deploy/crontab.stale-member-alert`` and asserted equal by
``tests/test_t_e5dcc325_stale_member_caller.py`` so code and schedule cannot
drift.

Manual runs (read-only, safe to repeat):

    cd ~/loopskill-api && set -a; . ./.env; set +a
    venv/bin/python -m app.crons.stale_member_alert          # human summary
    venv/bin/python -m app.crons.stale_member_alert --json    # machine-readable
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from sqlalchemy.orm import Session

from app.crons._cli import Parser
from app.services.stale_member_alert import (
    StaleMember,
    by_urgency,
    count_active_members,
    find_stale_members,
)

logger = logging.getLogger("loopskill.stale_member_alert")

# The schedule of record — single source of truth, mirrored byte-for-byte into
# deploy/crontab.stale-member-alert. WHY :15: the collector pings on a client-
# owned 1800s phase (NOT aligned to :00/:30 — POST /api/sync-report arrives on
# the member's own clock), so the sweep point cannot rescue a mis-calibrated
# row. A row still on the untouched 300s default has a 3×300=900s threshold
# against a 1800s cadence, and hosts whose phase lands above that read dark on
# EVERY sweep regardless of when we sample. The fix is data, not schedule: run
# scripts/repair_fleet_liveness_intervals.py --execute BEFORE installing this
# line (the standing predicate ~/.hermes/goals/fleet-stale-member-alert.md
# asserts the calibration stuck). flock -n -E 4 per the app/crons/_cli.py
# contract: a plain flock -n exits 1 SILENTLY on contention, which would read
# as "the sweeper crashed" — the package reserves 4 for exactly this, with a
# marker line so a skip leaves a trace.
CRONTAB_LINE = (
    "15 * * * * cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a; "
    "flock -n -E 4 /tmp/loopskill-stale-member-alert.lock "
    "venv/bin/python -m app.crons.stale_member_alert --json "
    ">> /home/wisechef/wiserecipes-logs/stale_member_alert.log 2>&1; "
    'rc=$?; [ $rc -ne 4 ] || echo \'{"skipped": "lock held by a live run"}\' '
    ">> /home/wisechef/wiserecipes-logs/stale_member_alert.log 2>&1; exit $rc"
)

# Exit codes. See the module docstring: 0-4 belong to app/crons/_cli.py, so the
# "dark hosts found" signal is 5 — a code no sibling job uses. 2 is this job's
# one legitimate use of the package "refused" code: a sweep that saw zero active
# members cannot attest that everyone is alive.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED_EMPTY_SCOPE = 2
EXIT_STALE_FOUND = 5

# Cap the alert sample so a fleet with hundreds of members cannot produce a
# log line / webhook body that a shipper truncates or a human abandons.
SAMPLE_LIMIT = 25


def summarize(stale: list[StaleMember], *, members_scanned: int) -> dict[str, Any]:
    """Machine-readable sweep result. Pure — no I/O, no clock.

    ``members_scanned`` is the denominator, and it is the reason an all-clear is
    auditable at all: ``stale_count: 0`` next to ``members_scanned: 0`` is not
    "everyone is alive", it is "this sweep looked at nothing" — the same false
    green that hid the last alert.
    """
    never_pinged = [s for s in stale if s.never_pinged]
    went_quiet = [s for s in stale if not s.never_pinged]
    sample = [s.payload() for s in by_urgency(stale)[:SAMPLE_LIMIT]]
    return {
        "members_scanned": members_scanned,
        "stale_count": len(stale),
        "never_pinged_count": len(never_pinged),
        "went_quiet_count": len(went_quiet),
        "sample": sample,
    }


def sweep(db: Session, *, fleet_id: str | None = None) -> dict[str, Any]:
    """Run the detector and shape it for whoever is on call. Writes nothing.

    ``fleet_id`` narrows to one fleet (tenant-scoped callers); ``None`` sweeps
    the whole deployment, which is what cron does.
    """
    stale = find_stale_members(db, fleet_id=fleet_id)
    report = summarize(stale, members_scanned=count_active_members(db, fleet_id=fleet_id))
    if report["members_scanned"] == 0:
        # The most dangerous string this file can produce is "no dark hosts"
        # from a sweep that saw nothing — a typo'd --fleet-id or a .env pointing
        # at the wrong database. Refuse the all-clear instead of granting it.
        logger.error(
            "STALE_MEMBER_SWEEP_REFUSED members_scanned=0 fleet_id=%s — cannot attest an all-clear",
            fleet_id,
        )
        return report
    if report["stale_count"] == 0:
        logger.info(
            "stale-member sweep: OK — no dark hosts (of %d active member(s))",
            report["members_scanned"],
        )
        return report
    # One line per finding, prefixed for grep AND for a log shipper's alert rule
    # ("STALE_MEMBER" is the token the standing predicate greps). Listing hosts
    # explicitly is the point: an alert that only says "3 stale" is not
    # actionable enough to survive contact with an on-call human. Iterated from
    # report["sample"], not a second slice of `stale`, so the log and the JSON
    # can never disagree about which hosts were named.
    for entry in report["sample"]:
        if entry["last_ping_at"] is None:
            ping_desc = "NEVER_PINGED"
        else:
            ping_desc = f"last_ping={entry['last_ping_at']} age={int(entry['seconds_since_ping'] or 0)}s"
        logger.warning(
            "STALE_MEMBER host=%s member_id=%s fleet_id=%s %s interval=%ds",
            entry["host"],
            entry["member_id"],
            entry["fleet_id"],
            ping_desc,
            entry["reconcile_interval_seconds"],
        )
    if report["stale_count"] > SAMPLE_LIMIT:
        logger.warning(
            "STALE_MEMBER ... and %d more (sample capped at %d)",
            report["stale_count"] - SAMPLE_LIMIT,
            SAMPLE_LIMIT,
        )
    logger.error(
        "STALE_MEMBER_SUMMARY stale=%d never_pinged=%d went_quiet=%d — a host went dark, do something",
        report["stale_count"],
        report["never_pinged_count"],
        report["went_quiet_count"],
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — this is what cron runs, so it is what the tests pin."""
    ap = Parser(
        prog="stale_member_alert",
        description="Sweep fleet members for liveness pings gone dark (read-only).",
    )
    ap.add_argument("--json", action="store_true", help="emit one machine-readable JSON line")
    ap.add_argument("--fleet-id", default=None, help="restrict the sweep to one fleet id")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Late import (the trick used by sync_report_pruner and crons/drain_last_used):
    # SessionLocal is resolved at CALL time, so a test that monkeypatches
    # app.database.SessionLocal lands on this call instead of being shadowed by
    # an import-time binding.
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        report = sweep(db, fleet_id=args.fleet_id)
        if args.json:
            print(json.dumps(report, default=str, sort_keys=True))
        if report["members_scanned"] == 0:
            # Saw nothing => cannot claim everyone is alive. Refuse, don't green.
            return EXIT_REFUSED_EMPTY_SCOPE
        return EXIT_STALE_FOUND if report["stale_count"] else EXIT_OK
    except Exception:  # noqa: BLE001 — Rationale: top-level cron; log-and-exit-nonzero
        logger.exception("stale_member_alert: unhandled error")
        return EXIT_ERROR
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
