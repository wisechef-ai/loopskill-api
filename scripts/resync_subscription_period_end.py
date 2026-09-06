"""Resync ``users.subscription_current_period_end`` from live Stripe state.

WHY THIS SCRIPT EXISTS
-----------------------
``_apply_subscription_state`` (app/subscription_service.py) previously read
``current_period_end`` only from the top level of the Stripe Subscription
object. On API versions >= 2025-03-31.basil (which includes the pinned
``2026-01-28.clover``), that field moved to ``items.data[].current_period_end``
and the top-level key is simply absent. Every row that received its LAST
webhook/checkout write under the buggy code has a stale, frozen
``subscription_current_period_end`` even though status/tier kept updating
correctly (entitlement was never affected — ``revenue_truth.py`` gates on
``status``, not ``period_end``).

The bug is fixed at the root in ``app/subscription_service.py``
(``_subscription_period_end``), which future webhook deliveries and checkout
reconciliation already pick up automatically. This script is a ONE-TIME
backfill for rows that drifted before the fix landed — it does not touch
anything the webhook path won't also correct on the row's next real event.

USAGE
-----
    python scripts/resync_subscription_period_end.py                 # dry run (default)
    python scripts/resync_subscription_period_end.py --dry-run
    python scripts/resync_subscription_period_end.py --apply

Never run --apply against prod from this task — dry-run only, reviewed by a
human, is the sanctioned path until Adam explicitly approves an apply run.

SCOPE
-----
Only users with ``subscription_id`` set are touched (rows with no live Stripe
subscription have nothing to resync). Each user is re-read live via
``stripe.Subscription.retrieve`` and run back through the SAME
``_apply_subscription_state`` used by the webhook and checkout-reconcile
paths — no hand-rolled field mapping, no drift between this script and
production logic.

IDEMPOTENCY
-----------
Re-running with --apply on an already-resynced fleet is a no-op: reading the
same Stripe subscription and calling ``_apply_subscription_state`` again
writes the identical value. Every row this run actually CHANGES gets one line
appended to ``state/subscription-resync.ledger.tsv`` (created if missing);
unchanged rows are printed but not ledgered, so the ledger is a pure audit
trail of what moved, not a run log.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import stripe

from app.config import settings
from app.database import SessionLocal
from app.models import User
from app.subscription_service import _apply_subscription_state, _subscription_period_end, _stripe_to_dict

LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "subscription-resync.ledger.tsv"


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return "NULL"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _append_ledger(line: str) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    """Dry-run (default) or apply the period_end resync. Returns a shell exit code."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print planned changes only (default behavior)")
    ap.add_argument("--apply", action="store_true", help="write the resolved period_end for drifted rows")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        print("ERROR: pass either --dry-run or --apply, not both")
        return 1
    apply_mode = args.apply

    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = "2026-01-28.clover"

    db = SessionLocal()
    changed = 0
    unchanged = 0
    errors = 0
    try:
        users = db.query(User).filter(User.subscription_id.isnot(None)).order_by(User.email).all()
        print(f"{'email':<45} {'db period_end':<30} {'stripe period_end':<30} would-change")
        for user in users:
            try:
                sub = stripe.Subscription.retrieve(user.subscription_id)
            except stripe.error.StripeError as exc:  # noqa: BLE001
                # Rationale: one user's unreachable/deleted Stripe subscription
                # must not abort the resync for every other row in the batch.
                print(f"{user.email:<45} ERROR retrieving {user.subscription_id}: {exc}")
                errors += 1
                continue

            sub_dict = _stripe_to_dict(sub)
            stripe_period_end_ts = _subscription_period_end(sub_dict)
            stripe_period_end = (
                datetime.fromtimestamp(stripe_period_end_ts, tz=UTC) if stripe_period_end_ts else None
            )
            db_period_end = user.subscription_current_period_end
            db_period_end_cmp = (
                db_period_end.replace(tzinfo=UTC)
                if db_period_end and db_period_end.tzinfo is None
                else db_period_end
            )
            would_change = db_period_end_cmp != stripe_period_end

            print(f"{user.email:<45} {_fmt(db_period_end):<30} {_fmt(stripe_period_end):<30} {would_change}")

            if not would_change:
                unchanged += 1
                continue

            if apply_mode:
                before = _fmt(db_period_end)
                _apply_subscription_state(user, sub_dict, db, event_ts=None)
                db.refresh(user)
                after = _fmt(user.subscription_current_period_end)
                _append_ledger(
                    f"{datetime.now(tz=UTC).isoformat()}\t{user.id}\t{user.email}\t"
                    f"{user.subscription_id}\t{before}\t{after}"
                )
            changed += 1

        print(
            f"\n{'APPLIED' if apply_mode else 'DRY RUN'}: {changed} would-change, {unchanged} unchanged, {errors} errors"
        )
        if not apply_mode:
            print("Rerun with --apply to write these changes.")
        return 0 if errors == 0 else 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
