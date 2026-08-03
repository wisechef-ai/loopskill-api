"""Purge the single fake creator_payouts row (hub D-018 #6, autopilot_0308 M0).

Adam confirmed prod's lone ``creator_payouts`` row is not real money — the
creator payout engine (D-013) has never actually paid anyone; the row is
left-over test/seed data. D-013 keeps the engine dormant and unscheduled
regardless; this script only removes the one contaminated row so a future
real payout run — once ``REVENUE_PER_INSTALL_CENTS`` is replaced by real
subscription attribution — starts from a clean table.

SAFETY
------
Dry-run by default. Refuses to touch the table at all unless it has
EXACTLY one row — a guard against this one-off script ever being pointed
at a table that has since accrued a real payout. Always prints the full
row before it would delete anything.

This script never connects to production; the orchestrator runs it there
after this PR merges. It uses whatever database ``app.database.SessionLocal``
resolves to in the environment it's run in.

USAGE
-----
    python scripts/purge_fake_creator_payout.py             # dry-run (default)
    python scripts/purge_fake_creator_payout.py --execute    # actually delete
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from app.database import SessionLocal
from app.models import CreatorPayout

REASON = "confirmed not real money (hub D-018 #6, Adam 2026-08-03)"


def _row_to_dict(row: CreatorPayout) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "creator_id": str(row.creator_id),
        "period_start": row.period_start.isoformat() if row.period_start else None,
        "period_end": row.period_end.isoformat() if row.period_end else None,
        "installs_count": row.installs_count,
        "gross_revenue_cents": row.gross_revenue_cents,
        "creator_share_cents": row.creator_share_cents,
        "currency": row.currency,
        "status": row.status,
        "stripe_transfer_id": row.stripe_transfer_id,
        "source": row.source,
        "amount_cents": row.amount_cents,
        "referral_id": str(row.referral_id) if row.referral_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
    }


def ledger_row(row_dict: dict[str, Any], reason: str = REASON) -> str:
    """Tab-separated row template for ~/.hermes/state/deletion-ledger.tsv.

    Columns: date, what, restore-path, reason.
    """
    date_str = datetime.now(UTC).date().isoformat()
    what = f"creator_payouts row {row_dict['id']}"
    restore_path = "none — not real money, nothing to restore"
    return f"{date_str}\t{what}\t{restore_path}\t{reason}"


def purge(db, *, execute: bool) -> int:
    """Delete the lone fake creator_payouts row. Returns a shell exit code.

    Refuses to act unless the table has EXACTLY one row.
    """
    count = db.query(CreatorPayout).count()
    if count != 1:
        print(
            f"REFUSING: creator_payouts has {count} row(s), expected exactly 1. "
            "This script is scoped to the single confirmed-fake row (hub D-018 #6) "
            "and will not touch a table that has grown since — that could be a "
            "real payout."
        )
        return 1

    row = db.query(CreatorPayout).first()
    row_dict = _row_to_dict(row)

    print("Row to delete:")
    for k, v in row_dict.items():
        print(f"  {k} = {v}")

    if not execute:
        print("\nDRY RUN — no rows deleted. Re-run with --execute to delete.")
        print("\nDeletion-ledger row (paste into ~/.hermes/state/deletion-ledger.tsv):")
        print(ledger_row(row_dict))
        return 0

    db.delete(row)
    db.commit()
    print("\nDeleted.")
    print("\nDeletion-ledger row (paste into ~/.hermes/state/deletion-ledger.tsv):")
    print(ledger_row(row_dict))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        return purge(db, execute=args.execute)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
