"""Grant (or revoke) a permanent complimentary subscription tier for a user.

WHY A SCRIPT AND NOT A HAND-TYPED UPDATE
----------------------------------------
Tier is read in several places, and every one of them gates on
``subscription_status in ("active", "trialing")`` *before* it even looks at
``subscription_tier`` (app/_skill_helpers.py, app/middleware/api_key.py,
app/forks_routes.py, app/discord_bot/role_sync.py). Setting only
``subscription_tier='pro'`` therefore does nothing at all — the status check
short-circuits first and the user stays effectively Free. Both columns must
move together.

THE CLOBBER TRAP
----------------
``GET /api/billing/me`` (app/checkout_routes.py) re-reconciles against Stripe
whenever ``stripe_customer_id`` is set AND (tier is None OR status is unhealthy).
A comp that sets status='active' makes ``needs_reconcile`` False, so the
reconciler never runs and never overwrites the comp. That is what makes this
durable rather than a value that silently reverts on the user's next page load.

The remaining write path is the Stripe *webhook*
(``handle_subscription_event``), which resolves the user by
``metadata.wiserecipes_user_id`` or ``stripe_customer_id`` and would clobber the
comp on any future subscription event for that customer. ``--detach-stripe``
clears ``stripe_customer_id`` so no future webhook can resolve to this row. Use
it for a true permanent comp on an account with a dead/canceled Stripe customer.
Do NOT use it on an account that is expected to pay again later.

PERMANENCE
----------
``subscription_current_period_end`` is left NULL. Nothing in the codebase
expires a subscription by comparing that column to now(); it is only rendered in
API responses and used by subscriber_credit_service as a credit-expiry base
(which already falls back safely when NULL). NULL is therefore the correct
encoding for "never expires" and is what makes this comp permanent.

USAGE
-----
    python scripts/grant_comp_tier.py --email a@b.com --tier pro            # dry run
    python scripts/grant_comp_tier.py --email a@b.com --tier pro --apply
    python scripts/grant_comp_tier.py --email a@b.com --tier pro --apply --detach-stripe
    python scripts/grant_comp_tier.py --email a@b.com --revoke --apply
"""

from __future__ import annotations

import argparse
import sys

from app.database import SessionLocal
from app.models import User

VALID_TIERS = ("free", "pro", "pro_plus")


def main() -> int:
    """Grant or revoke a comp tier. Returns a shell exit code."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--tier", default="pro", choices=VALID_TIERS)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--revoke", action="store_true", help="remove the comp (back to free)")
    ap.add_argument(
        "--detach-stripe",
        action="store_true",
        help="clear stripe_customer_id so no future webhook can clobber the comp",
    )
    args = ap.parse_args()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == args.email).first()
        if user is None:
            print(f"ERROR: no user with email {args.email}")
            return 1

        print("BEFORE:")
        print(f"  id                 = {user.id}")
        print(f"  email              = {user.email}")
        print(f"  subscription_tier  = {user.subscription_tier!r}")
        print(f"  subscription_status= {user.subscription_status!r}")
        print(f"  subscription_id    = {user.subscription_id!r}")
        print(f"  stripe_customer_id = {user.stripe_customer_id!r}")
        print(f"  period_end         = {user.subscription_current_period_end!r}")

        if args.revoke:
            new_tier: str | None = None
            new_status = "canceled"
        else:
            new_tier = args.tier
            new_status = "active"

        print("\nPLANNED:")
        print(f"  subscription_tier  -> {new_tier!r}")
        print(f"  subscription_status-> {new_status!r}")
        print("  subscription_current_period_end -> None  (never expires)")
        print("  subscription_id    -> None  (no Stripe subscription backs a comp)")
        if args.detach_stripe:
            print("  stripe_customer_id -> None  (webhook can no longer resolve this row)")

        if not args.apply:
            print("\nDRY RUN — rerun with --apply to write")
            return 0

        user.subscription_tier = new_tier
        user.subscription_status = new_status
        user.subscription_current_period_end = None
        user.subscription_id = None
        if args.detach_stripe:
            user.stripe_customer_id = None
        db.commit()
        db.refresh(user)

        print("\nAFTER (read back from DB):")
        print(f"  subscription_tier  = {user.subscription_tier!r}")
        print(f"  subscription_status= {user.subscription_status!r}")
        print(f"  subscription_id    = {user.subscription_id!r}")
        print(f"  stripe_customer_id = {user.stripe_customer_id!r}")
        print(f"  period_end         = {user.subscription_current_period_end!r}")

        healthy = user.subscription_status in ("active", "trialing")
        print(f"\ngates on status in (active,trialing): {'PASS' if healthy else 'FAIL'}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
