#!/usr/bin/env python3
"""t_e5dcc325 — calibrate fleet_member_liveness intervals that never were declared.

## Why these rows are wrong (measured, prod, 2026-11-27)

Three live members (``adam-xps``, ``mac01``, ``wisechef-agents``) carry
``reconcile_interval_seconds = 300``, stamped 2026-07-16 20:16:28Z by the model
default — ``Column(..., default=300, server_default="300")`` — not by an owner.
Their ONLY ping writer since PR #496 (merged 2026-11-27, ``c2afc30``) is the
*/30 collector's accepted sync-report, which declares 1800 on row CREATION
(``SYNC_REPORT_RECONCILE_INTERVAL_SECONDS``) and deliberately never overwrites
an existing value.

The consequence is a permanent false alarm. The stale-member alert fires at
``STALE_MULTIPLE`` (3) × the declared interval:

    declared 300s × 3 = 900s threshold, against a 1800s ping cadence.

So every correctly-alive host reads "dark" for 15 minutes of every 30-minute
cycle — 50% of the time, forever. A caller wired on top of that data cries wolf
on all three healthy hosts and gets muted within a day, which is a worse
outcome than no alert at all. This repair is therefore not cosmetic: it is what
makes the caller in ``app/crons/stale_member_alert.py`` survivable.

## Why this is not "silently rewriting owner data"

* It is **explicit** — a named script, dry-run by default, ``--execute`` required.
* It is **scoped** — ``FleetMember.host`` is unique only per
  ``(fleet_id, host, profile)`` (``app/models.py``), so a bare hostname is NOT a
  tenant boundary. ``--execute`` against ``--host`` therefore REQUIRES
  ``--fleet-id``; without it one tenant's repair could calibrate another
  tenant's rows. ``--member-id`` targets are primary keys and always safe.
* It is **narrow** — only rows still sitting on the untouched server default
  (exactly 300). A value an owner consciously declared (900, 7200, anything but
  the default) is never a candidate, and the script refuses it. That predicate
  is re-asserted **inside the UPDATE**, not just in the plan read:
  ``loopskill_ping`` (``app/mcp/tools/fleet_ingest.py``) is a live concurrent
  writer of this very column, so an owner declaration landing between the plan
  and the commit makes the UPDATE match zero rows rather than clobber it.
* It is **targeted** — ``--execute`` with no ``--member-id``/``--host`` refuses
  with exit 2 (the ``set_fleet_origin.py --execute`` lesson from t_d0b42dfa: a
  bulk mutation that defaults to "everything" is a bug, not a convenience).
* It is **auditable** — ``--execute`` REQUIRES ``--backup-dir``, and the
  before/after snapshot (plus a sha256 ``CHECKSUMS.txt``) is written BEFORE the
  commit, so there is a restore point even if the write itself explodes. A
  ``state/fleet-liveness-repair.ledger.tsv`` row is appended and echoed to
  stdout, matching the discipline of ``scripts/resync_subscription_period_end.py``.
* It is **what the code already believes** — 1800 is the constant PR #496 stamps
  for this exact population; the repair only aligns rows that predate it.

## Usage

    cd ~/loopskill-api && set -a; . ./.env; set +a
    venv/bin/python scripts/repair_fleet_liveness_intervals.py            # dry run
    venv/bin/python scripts/repair_fleet_liveness_intervals.py \
        --execute --fleet-id c715f606-3aa5-4453-885a-53a3ddbe7fd5 \
        --host adam-xps --host mac01 --host wisechef-agents \
        --reason "t_e5dcc325: server-default 300 vs */30 collector cadence" \
        --backup-dir ~/.hermes/backups/fleet-liveness-intervals-t_e5dcc325

Exit codes: 0 = ok (including "nothing to do"), 2 = refused (no target, no
reason, no backup-dir, a host target without --fleet-id, an unparseable
--fleet-id, or a target that is not found), 1 = unexpected error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

if __package__ in (None, ""):  # run directly (`python scripts/x.py`): make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import FleetMember, FleetMemberLiveness
from app.services.sync_report import SYNC_REPORT_RECONCILE_INTERVAL_SECONDS

logger = logging.getLogger("loopskill.repair_fleet_liveness_intervals")

# The untouched server default from app/models.py::FleetMemberLiveness. A row
# carrying this value has NOT had its interval declared by anyone — it is what
# the column got for free. Anything else is an owner's decision and is off-limits.
UNTOUCHED_SERVER_DEFAULT = 300

# Durable audit trail. Deliberately NOT under stdout: a `> run.json` capture must
# not be able to swallow the only record that a prod row changed (ocr finding,
# t_e5dcc325 — the first draft logged it, which stderr-redirection silently ate).
LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "fleet-liveness-repair.ledger.tsv"


def _append_ledger(line: str) -> None:
    """Append one TSV row, sibling pattern to scripts/resync_subscription_period_end.py."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_candidates(
    db: Session,
    *,
    targets: set[str],
    fleet_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Rows that are safe to calibrate, as before/after records.

    A candidate must be: an ACTIVE member, named in ``targets``, optionally
    inside ``fleet_id``. The join is an OUTER one on purpose (ocr r2): an
    active member with NO liveness row (the astrovita class — the alert's own
    headline case) must be reported as "member exists, no liveness row —
    nothing to calibrate", NOT dumped into "targets not found", which would
    send whoever triages the alert chasing hostnames and fleet ids that are correct.

    ``fleet_id`` is not an optimisation. ``host`` is unique only per
    ``(fleet_id, host, profile)``, so a hostname alone does not identify one
    tenant's member — without this filter a repair aimed at one client can
    reach another client's rows.
    """
    q = (
        db.query(FleetMember, FleetMemberLiveness)
        .outerjoin(FleetMemberLiveness, FleetMemberLiveness.member_id == FleetMember.id)
        .filter(FleetMember.is_active == True)  # noqa: E712
    )
    if fleet_id is not None:
        q = q.filter(FleetMember.fleet_id == fleet_id)
    out: list[dict[str, Any]] = []
    for member, lv in q.all():
        if member.host not in targets and str(member.id) not in targets:
            continue
        out.append(
            {
                "member_id": str(member.id),
                "host": member.host,
                "fleet_id": str(member.fleet_id),
                "has_liveness_row": lv is not None,
                "was": lv.reconcile_interval_seconds if lv is not None else None,
                "would_be": SYNC_REPORT_RECONCILE_INTERVAL_SECONDS if lv is not None else None,
                "last_ping_at": lv.last_ping_at.isoformat() if lv is not None and lv.last_ping_at else None,
                "safe": lv is not None and lv.reconcile_interval_seconds == UNTOUCHED_SERVER_DEFAULT,
            }
        )
    return out


def apply_changes(db: Session, changes: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Conditional writes. Returns ``(rows_actually_changed, skipped_member_ids)``.

    The safety predicate is repeated in the WHERE clause rather than trusted from
    the plan read. ``reconcile_interval_seconds`` has a live concurrent writer —
    ``loopskill_ping`` assigns it on every owner ping — so a plan-then-assign
    would clobber a value the owner declared in between. Re-querying through the
    identity map would not help either (it issues no SQL), which is why this is a
    bulk UPDATE with the guard in the predicate and the answer taken from the
    rowcount the database itself reports.
    """
    changed = 0
    skipped: list[str] = []
    for r in changes:
        rowcount = (
            db.query(FleetMemberLiveness)
            .filter(
                # Uuid column: compare on a UUID object, not the str used in the
                # plan (the SQLite leg gives the two different affinities — PR #489).
                FleetMemberLiveness.member_id == UUID(r["member_id"]),
                FleetMemberLiveness.reconcile_interval_seconds == UNTOUCHED_SERVER_DEFAULT,
            )
            .update(
                {"reconcile_interval_seconds": r["would_be"]},
                synchronize_session=False,
            )
        )
        if rowcount:
            changed += int(rowcount)
        else:
            # Row vanished, or an owner declared a value after the plan read.
            # Either way it is not ours to write.
            skipped.append(r["member_id"])
    db.commit()
    return changed, skipped


def repair(
    db: Session,
    *,
    targets: set[str],
    execute: bool,
    reason: str,
    fleet_id: UUID | None = None,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """Calibrate the targeted rows. Dry-run unless ``execute`` is set.

    Returns the full plan (including rows refused for not being the untouched
    default) so the caller can log and ledger it.
    """
    plan = find_candidates(db, targets=targets, fleet_id=fleet_id)
    missing = targets - {r["host"] for r in plan} - {r["member_id"] for r in plan}
    changes = [r for r in plan if r["safe"]]
    refused = [r for r in plan if not r["safe"] and r["has_liveness_row"]]
    # Active member, target matched, NO fleet_member_liveness row at all — the
    # astrovita class. Not a typo and not a calibration job: there is nothing to
    # stamp. Reported separately so whoever reads the log sees "provisioning gap", the
    # same diagnosis find_stale_members gives (never_pinged).
    no_liveness = [r for r in plan if not r["has_liveness_row"]]

    report: dict[str, Any] = {
        "executed": execute,
        "reason": reason,
        "fleet_id": str(fleet_id) if fleet_id else None,
        "target_interval_seconds": SYNC_REPORT_RECONCILE_INTERVAL_SECONDS,
        "planned": len(changes),
        "changed": 0,
        "changed_member_ids": [],
        "changes": [],
        "skipped_raced": [],
        "refused_not_untouched_default": refused,
        "no_liveness_row": no_liveness,
        "targets_not_found": sorted(missing),
        "rows": plan,
    }

    if not execute:
        logger.info(
            "dry-run: %d row(s) would be calibrated, %d refused, %d without a liveness row",
            len(changes),
            len(refused),
            len(no_liveness),
        )
        return report

    if changes:
        # Backup BEFORE the write. A snapshot taken after db.commit() is not a
        # restore point — it is a transcript of damage already done — and it is
        # lost entirely if the commit raises.
        if backup_dir is None:
            raise RuntimeError("--execute requires --backup-dir so a restore point exists")
        report["backup"] = write_backup(report, backup_dir)
        changed, skipped = apply_changes(db, changes)
        skipped_set = set(skipped)
        landed = [r for r in changes if r["member_id"] not in skipped_set]
        report["changed"] = changed
        report["skipped_raced"] = skipped
        # Self-sufficient audit (ocr r2): the durable ledger must answer "which
        # prod rows did this run touch, from what to what" WITHOUT requiring the
        # hand-picked --backup-dir to have survived.
        report["changed_member_ids"] = [r["member_id"] for r in landed]
        report["changes"] = [f"{r['host']}:{r['was']}->{r['would_be']}" for r in landed]
        logger.info(
            "repair: CALIBRATED %d row(s) to %ds (%d skipped as no-longer-safe) — %s",
            changed,
            SYNC_REPORT_RECONCILE_INTERVAL_SECONDS,
            len(skipped),
            reason,
        )
    return report


def write_backup(report: dict[str, Any], backup_dir: str) -> dict[str, str]:
    """Persist a before/after snapshot + checksum outside scratch.

    Called BEFORE the commit. Scratch is pruned; an audit trail that evaporates
    with the cleanup is not an audit trail. Written next to the pg_dump taken
    before --execute.
    """
    out_dir = Path(backup_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"fleet_member_liveness-{stamp}.json"
    payload = json.dumps(report, indent=2, sort_keys=True, default=str)
    path.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with (out_dir / "CHECKSUMS.txt").open("a", encoding="utf-8") as f:
        f.write(f"{digest}  {path.name}\n")
    logger.info("backup written before commit: %s sha256=%s", path, digest)
    return {"path": str(path), "sha256": digest}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="repair_fleet_liveness_intervals",
        description="Calibrate never-declared fleet_member_liveness intervals (dry-run by default).",
    )
    ap.add_argument("--member-id", action="append", default=[], help="target a member id (repeatable)")
    ap.add_argument("--host", action="append", default=[], help="target a member host (repeatable)")
    ap.add_argument(
        "--fleet-id",
        default=None,
        help="restrict targets to one fleet; REQUIRED with --execute + --host (host is only unique per fleet)",
    )
    ap.add_argument("--execute", action="store_true", help="actually write (refuses without a target)")
    ap.add_argument(
        "--reason", default="", help="why this mutation is happening (recorded in the ledger row)"
    )
    ap.add_argument(
        "--backup-dir",
        default=None,
        help="write a before/after snapshot + CHECKSUMS.txt here (REQUIRED with --execute)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    targets = set()
    for t in set(args.member_id) | set(args.host):
        # Member ids may arrive uppercase from a UI/log; str(UUID) canonicalises
        # so both id options behave alike (fleet-id is already UUID-normalized).
        # Hosts stay verbatim — an unparseable string IS a legitimate hostname.
        try:
            targets.add(str(UUID(t)))
        except (ValueError, AttributeError, TypeError):
            targets.add(t)

    def _refuse(msg: str) -> int:
        logger.error("%s — refusing to mutate anything", msg)
        return 2

    if not args.execute:
        if args.fleet_id is None and args.host:
            logger.warning(
                "dry-run over --host targets is unscoped: 'host' is unique only per "
                "(fleet_id, host, profile), so pass --fleet-id to see one tenant's rows"
            )

    fleet_uuid: UUID | None = None
    if args.fleet_id is not None:
        try:
            fleet_uuid = UUID(str(args.fleet_id))
        except (ValueError, TypeError):
            # Scoping to "every fleet" on a typo would be the cross-tenant
            # failure this option exists to prevent, so it is an error, not a
            # warning — and never a fallback to unscoped.
            return _refuse(f"--fleet-id {args.fleet_id!r} is not a UUID")

    if not args.execute:
        pass  # dry run writes nothing; the warnings above are enough
    elif not targets:
        # A bulk interval mutation with no target is not a repair, it is an
        # accident waiting for a typo (t_d0b42dfa).
        return _refuse("--execute requires at least one --member-id or --host target")
    elif args.host and fleet_uuid is None:
        # HIGH (ocr, t_e5dcc325): host is not a tenant boundary. Executing on a
        # hostname could calibrate another client's member with the same name.
        return _refuse("--execute with --host requires --fleet-id (host is unique only per fleet)")
    elif not args.reason.strip():
        return _refuse("--execute requires --reason (this row change must be explainable later)")
    elif not args.backup_dir:
        return _refuse("--execute requires --backup-dir (a restore point must exist before any write)")

    from app.database import SessionLocal  # late import — testability, see crons/*

    db = SessionLocal()
    try:
        report = repair(
            db,
            targets=targets,
            execute=args.execute,
            reason=args.reason,
            fleet_id=fleet_uuid,
            backup_dir=args.backup_dir,
        )
        print(json.dumps(report, sort_keys=True, default=str))

        if args.execute and report["changed"]:
            # A real artifact, not a log line: stdout capture and stderr
            # redirection must not be able to swallow the record. Reason is
            # repr()'d so the spaces in a sentence cannot split the TSV fields.
            # member_ids/changes make the durable ledger self-sufficient (ocr r2):
            # "which prod rows did this run touch, from what to what" must be
            # answerable even if the --backup-dir chosen at run time is long gone.
            ledger = "\t".join(
                [
                    datetime.now(timezone.utc).isoformat(),
                    "actor=repair_fleet_liveness_intervals",
                    "action=update",
                    "table=fleet_member_liveness",
                    f"rows={report['changed']}",
                    f"member_ids={','.join(report['changed_member_ids'])}",
                    f"changes={';'.join(report['changes'])}",
                    f"fleet_id={report['fleet_id']}",
                    f"reason={args.reason!r}".replace("\n", "\\n").replace("\t", " "),
                    f"backup={report.get('backup', {}).get('path', 'NONE-TAKEN')}",
                ]
            )
            _append_ledger(ledger)
            logger.info("LEDGER %s", ledger)
            print(ledger)  # capturable, mirrors scripts/resync_subscription_period_end.py
        if args.execute and report["skipped_raced"]:
            logger.warning(
                "%d row(s) stopped being safe between plan and write (an owner declared a value, "
                "or the member vanished) and were NOT touched: %s",
                len(report["skipped_raced"]),
                ", ".join(report["skipped_raced"]),
            )
        if report["targets_not_found"]:
            logger.error(
                "targets not found (typo, inactive, or outside --fleet-id): %s",
                ", ".join(report["targets_not_found"]),
            )
            return 2
        if report["refused_not_untouched_default"]:
            # These rows carry a value someone chose. Hand-editing them is the
            # human's call to make knowingly, not this script's to make silently.
            logger.warning(
                "refused %d row(s) whose interval is not the untouched server default: %s",
                len(report["refused_not_untouched_default"]),
                ", ".join(f"{r['host']}={r['was']}" for r in report["refused_not_untouched_default"]),
            )
        if report["no_liveness_row"]:
            # The astrovita class: active member, correct target, NO liveness row.
            # Distinct from "targets not found" on purpose — whoever chases a
            # STALE_MEMBER alert must not be told their correct hostname is a typo.
            logger.warning(
                "%d target(s) are active but have NO fleet_member_liveness row — nothing to "
                "calibrate; this is the never-pinged dark-host case (provisioning gap), not a typo: %s",
                len(report["no_liveness_row"]),
                ", ".join(r["host"] for r in report["no_liveness_row"]),
            )
        return 0
    except Exception:  # noqa: BLE001 — Rationale: ops script; log-and-exit-nonzero
        logger.exception("repair_fleet_liveness_intervals: unhandled error")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
