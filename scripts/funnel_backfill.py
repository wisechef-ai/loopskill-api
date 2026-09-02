#!/usr/bin/env python3
"""scripts/funnel_backfill.py — flywheel_0902/B funnel-ledger backfill CLI.

Idempotent (safe to re-run). DRY-RUN BY DEFAULT — pass --live to write.

Usage:
  python3 scripts/funnel_backfill.py                       # dry-run, no Stripe
  python3 scripts/funnel_backfill.py --live                 # write, no Stripe
  python3 scripts/funnel_backfill.py --live --with-stripe   # write incl. paid stage
  python3 scripts/funnel_backfill.py --host chef            # override host tag

Requires WR_DATABASE_URL (or DATABASE_URL) pointing at the target database.
--with-stripe additionally requires WR_STRIPE_SECRET_KEY / STRIPE_SECRET_KEY.

Exit codes:
  0  backfill completed (dry-run or live)
  1  fatal error (bad DB url, Stripe auth failure, etc.)
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fetch_stripe_paid_sources() -> tuple[list[dict], list[dict]]:
    """Pull paid invoices + succeeded payment_intents from live Stripe.

    Requires the runtime `stripe` SDK (15.x — .to_dict() per the repo's
    Stripe SDK convention, not the deprecated dict-style access).
    """
    import stripe

    sk = (os.environ.get("WR_STRIPE_SECRET_KEY") or os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not sk:
        print("ERROR: WR_STRIPE_SECRET_KEY / STRIPE_SECRET_KEY not set — cannot fetch Stripe data.")
        sys.exit(1)
    stripe.api_key = sk

    invoices = [inv.to_dict() for inv in stripe.Invoice.list(status="paid", limit=100).auto_paging_iter()]
    payment_intents = [
        pi.to_dict() for pi in stripe.PaymentIntent.list(limit=100).auto_paging_iter() if pi.status == "succeeded"
    ]
    return invoices, payment_intents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="Actually write rows (default: dry-run).")
    parser.add_argument(
        "--with-stripe", action="store_true", help="Also backfill the 'paid' stage from live Stripe."
    )
    parser.add_argument("--host", default=socket.gethostname(), help="Host tag for written rows.")
    args = parser.parse_args()

    dry_run = not args.live

    # Local imports AFTER sys.path setup, so `python3 scripts/funnel_backfill.py`
    # works from any cwd without an installed package.
    from app.database import SessionLocal
    from app.services.funnel_backfill import run_full_backfill

    invoices: list[dict] | None = None
    payment_intents: list[dict] | None = None
    if args.with_stripe:
        invoices, payment_intents = _fetch_stripe_paid_sources()

    db = SessionLocal()
    try:
        results = run_full_backfill(
            db,
            host=args.host,
            invoices=invoices,
            payment_intents=payment_intents,
            dry_run=dry_run,
        )
    finally:
        db.close()

    print(f"funnel_backfill — {'DRY-RUN (no writes)' if dry_run else 'LIVE'} — host={args.host}")
    for result in results:
        print(
            f"  {result.stage:16s} scanned={result.scanned:6d} written={result.written:6d} "
            f"replayed={result.replayed:6d}"
        )
        for sample in result.sample:
            print(f"      sample: {sample}")

    if dry_run:
        print(
            "\nDry-run only — no rows written. Re-run with --live (and --with-stripe "
            "for the paid stage) to commit."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
