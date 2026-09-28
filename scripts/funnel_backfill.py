#!/usr/bin/env python3
"""scripts/funnel_backfill.py — flywheel_0902/B funnel-ledger backfill CLI.

Idempotent (safe to re-run). DRY-RUN BY DEFAULT — pass --live to write.

Usage:
  python3 scripts/funnel_backfill.py                       # dry-run, no Stripe
  python3 scripts/funnel_backfill.py --live                 # write, no Stripe
  python3 scripts/funnel_backfill.py --live --with-stripe   # write incl. paid stage
  python3 scripts/funnel_backfill.py --host chef            # override host tag
  python3 scripts/funnel_backfill.py --live --reclassify-installs
      # pricing0928: re-classify existing installed:stranger rows (hosting IP
      # -> unknown, probe/coldstart-bench -> fleet)
  python3 scripts/funnel_backfill.py --live --with-stripe --prune-onetime-dupes
      # pricing0928: delete stripe-onetime paid rows that duplicate an invoice

Scheduling: this script is the ONLY writer of installed/signup/bundle_created
funnel rows. It ran once by hand on 2026-09-02 and never again, which is why
installed:stranger froze on that date. Run it daily (idempotent), e.g.
  15 3 * * * cd <repo> && venv/bin/python scripts/funnel_backfill.py --live

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--live", action="store_true", help="Actually write rows (default: dry-run).")
    parser.add_argument(
        "--with-stripe", action="store_true", help="Also backfill the 'paid' stage from live Stripe."
    )
    parser.add_argument("--host", default=socket.gethostname(), help="Host tag for written rows.")
    parser.add_argument(
        "--reclassify-installs",
        action="store_true",
        help="Re-classify existing installed:stranger rows with the current rules.",
    )
    parser.add_argument(
        "--prune-onetime-dupes",
        action="store_true",
        help="Delete stripe-onetime paid rows that duplicate an invoice (needs --with-stripe).",
    )
    args = parser.parse_args(argv)
    if args.prune_onetime_dupes and not args.with_stripe:
        parser.error("--prune-onetime-dupes needs --with-stripe")

    dry_run = not args.live

    # Local imports AFTER sys.path setup, so `python3 scripts/funnel_backfill.py`
    # works from any cwd without an installed package.
    from app.database import SessionLocal
    from app.services.funnel_backfill import (
        prune_invoice_backed_onetime,
        reclassify_hosting_installs,
        run_full_backfill,
    )

    invoices: list[dict] | None = None
    payment_intents: list[dict] | None = None
    if args.with_stripe:
        invoices, payment_intents = _fetch_stripe_paid_sources()

    db = SessionLocal()
    try:
        corrections = []
        # Corrections first, so the paid backfill below never re-adds a pruned row
        # (it dedupes with the same rule) and reports reflect the corrected ledger.
        if args.reclassify_installs:
            corrections.append(reclassify_hosting_installs(db, dry_run=dry_run))
        if args.prune_onetime_dupes:
            corrections.append(
                prune_invoice_backed_onetime(
                    db, invoices=invoices or [], payment_intents=payment_intents or [], dry_run=dry_run
                )
            )
        results = corrections + run_full_backfill(
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
