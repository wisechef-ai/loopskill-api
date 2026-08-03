#!/usr/bin/env python3
"""autopilot_0308 M2 — migrate the 5 live `pro_plus` users to `pro` (D-010).

Adam verbatim: "migrate to pro." Not grandfathered at $100, not converted to
Enterprise. This is a REVENUE-TOUCHING script (premortem risk #1, the
highest-scored risk in the sprint) — read this whole docstring before running
it against anything but a seeded test DB.

## What this does

For every user whose `subscription_tier` is `pro_plus` (canonicalized —
legacy `operator`/`studio` rows are included defensively even though the
live rename is complete), moves their Stripe subscription onto the `pro`
price with `proration_behavior=create_prorations` and updates
`subscription_tier` to `"pro"`. Reuses
`app.subscription_service.downgrade_pro_plus_to_pro` verbatim — the same
call sequence already live behind `POST /api/subscriptions/downgrade`
(self-serve downgrade) — rather than inventing a second Stripe interaction
for the same state transition.

Because `pro` ($9.95/mo) is cheaper than `pro_plus` ($100/mo), the proration
credit means every migrated user pays LESS going forward. That is the honest
framing for the comms draft — lead with it.

## What this does NOT do

- **Never archives or deactivates the `pro_plus` Stripe price.** Only
  `Subscription.modify` is called (moves a subscription off the price); the
  price object itself is never touched. Per the `stripe-live-price-rotation`
  skill: never deactivate a price with live subscriptions still attached,
  and until every affected user here has actually run, the pro_plus price
  still has subscriptions on it.
- **Never drops the `pro_plus` db_slug.** It stays valid indefinitely — for
  the migration window and for Enterprise contracts afterwards.
- **Never runs by itself.** Dry-run is the default and the only mode that
  needs no confirmation. `--execute` is gated behind BOTH the flag and a
  typed confirmation read from stdin — see below.

## Safety properties

- **Dry-run by default.** No flag, no writes, ever.
- **`--execute` requires a typed confirmation.** After printing the plan,
  the script prints the exact phrase to type (`MIGRATE <n> USERS`) and reads
  it from stdin. A flag alone can never trigger a write.
- **Idempotent.** Each user is re-checked against the DB immediately before
  being migrated; a user who is no longer `pro_plus` (already migrated by an
  earlier partial run) is skipped, not re-migrated or double-charged.
- **Fails closed on drift.** Immediately after the typed confirmation is
  read, the affected-user set is recomputed. If it differs at all from what
  was confirmed (a signup, a cancellation, anything), the whole run aborts
  without writing a single row — re-run to see the current plan and confirm
  again.

## Usage

    python scripts/migrate_pro_plus_to_pro.py              # dry run (default)
    python scripts/migrate_pro_plus_to_pro.py --execute     # prompts for confirmation, then writes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from app import subscription_service as ss  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402

# Legacy tier slugs that canonicalize to pro_plus — included defensively.
# RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
_PRO_PLUS_LIKE_SLUGS = ("pro_plus", "operator", "studio")

CONFIRMATION_TEMPLATE = "MIGRATE {n} USERS"


# ── Plan (read-only) ──────────────────────────────────────────────────────


def find_pro_plus_users(db: Session) -> list[User]:
    """Every user whose canonical tier is pro_plus, ordered by id for a
    deterministic plan across dry-run and execute."""
    candidates = (
        db.query(User).filter(User.subscription_tier.in_(_PRO_PLUS_LIKE_SLUGS)).order_by(User.id).all()
    )
    return [u for u in candidates if ss._normalise_tier(u.subscription_tier) == "pro_plus"]


def build_plan(db: Session) -> list[dict[str, Any]]:
    """One row per affected user: id, email, current tier, Stripe subscription
    id, current price, target price. Read-only — never writes."""
    pro_price_usd = ss.TIER_USD_PRICE.get("pro")
    plan = []
    for u in find_pro_plus_users(db):
        plan.append(
            {
                "user_id": u.id,
                "email": u.email,
                "current_tier": u.subscription_tier,
                "stripe_subscription_id": u.subscription_id,
                "current_price_usd": ss.TIER_USD_PRICE.get(ss._normalise_tier(u.subscription_tier) or ""),
                "target_tier": "pro",
                "target_price_usd": pro_price_usd,
            }
        )
    return plan


def _fmt_usd(amount: float | None) -> str:
    return f"${amount:,.2f}" if amount is not None else "(unknown)"


def print_plan(plan: list[dict[str, Any]]) -> None:
    if not plan:
        print("no pro_plus users found — nothing to migrate")
        return
    print(f"{len(plan)} pro_plus user(s) affected:\n")
    for p in plan:
        print(f"  user_id             = {p['user_id']}")
        print(f"  email               = {p['email'] or '(no email)'}")
        print(f"  current_tier        = {p['current_tier']!r}")
        print(f"  stripe_subscription = {p['stripe_subscription_id'] or '(none)'}")
        print(f"  current_price       = {_fmt_usd(p['current_price_usd'])}/mo")
        print(
            f"  target_price        = {_fmt_usd(p['target_price_usd'])}/mo (target_tier={p['target_tier']!r})"
        )
        print()


def confirmation_phrase(n: int) -> str:
    return CONFIRMATION_TEMPLATE.format(n=n)


# ── Execute (writes — only reached behind --execute + confirmation) ───────


def migrate_one(db: Session, user_id: UUID) -> dict[str, Any]:
    """Migrate a single user. Idempotent: re-checks live DB state first —
    a user who is no longer pro_plus (already migrated by a prior partial
    run) is skipped, not re-migrated or double-charged."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        print(f"  ERROR  {user_id}: user no longer exists")
        return {"user_id": str(user_id), "status": "error", "detail": "user_not_found"}

    before = {"tier": user.subscription_tier, "subscription_id": user.subscription_id}
    label = user.email or str(user.id)

    if ss._normalise_tier(user.subscription_tier) != "pro_plus":
        print(f"  SKIP   {label}: already {user.subscription_tier!r} — not re-migrating (idempotent)")
        return {
            "user_id": str(user.id),
            "status": "skipped_already_migrated",
            "before": before,
            "after": before,
        }

    if not user.subscription_id:
        print(f"  SKIP   {label}: no Stripe subscription id — needs manual review, not touched")
        return {
            "user_id": str(user.id),
            "status": "skipped_no_subscription",
            "before": before,
            "after": before,
        }

    try:
        result = ss.downgrade_pro_plus_to_pro(user, db)
    # Rationale: a raw Stripe API/network error must not abort the whole batch —
    # one user's failure is reported and the rest still migrate; re-running the
    # script later picks up exactly this user (idempotent).
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL   {label}: {e}")
        return {
            "user_id": str(user.id),
            "status": "error",
            "detail": str(e),
            "before": before,
            "after": before,
        }

    after = {"tier": user.subscription_tier, "subscription_id": user.subscription_id}
    print(
        f"  OK     {label}: {before['tier']} -> {after['tier']} "
        f"(sub {after['subscription_id']}, stripe_status={result.get('stripe_status')})"
    )
    return {"user_id": str(user.id), "status": "migrated", "before": before, "after": after}


def run_migration(db: Session, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the plan. Each user is migrated independently (no shared
    transaction) so a failure partway through leaves already-migrated users
    migrated — safe to just re-run the script for the remainder."""
    return [migrate_one(db, p["user_id"]) for p in plan]


# ── CLI ──────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--execute",
        action="store_true",
        help="write changes (default: dry-run). Still requires a typed confirmation.",
    )
    return ap


def main(argv: list[str] | None = None, confirm_reader=input, db: Session | None = None) -> int:
    """CLI entry point.

    ``db`` is normally left None — production usage opens (and closes) its
    own SessionLocal(). Tests inject an in-memory session directly and own
    its lifecycle instead, so ownership is tracked explicitly rather than
    always closing whatever session was passed in.
    """
    args = build_arg_parser().parse_args(argv)

    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        plan = build_plan(db)
        print("== pro_plus -> pro migration (D-010) ==\n")
        print_plan(plan)

        if not args.execute:
            print("DRY RUN — no changes written. Re-run with --execute to migrate.")
            return 0

        if not plan:
            print("Nothing to migrate — exiting.")
            return 0

        expected_n = len(plan)
        confirmed_ids = {p["user_id"] for p in plan}
        phrase = confirmation_phrase(expected_n)
        print(f"This will migrate {expected_n} user(s) from pro_plus to pro (proration applies).")
        print("The pro_plus Stripe price is NOT deactivated by this script.")
        print(f'Type exactly "{phrase}" to proceed:')
        typed = (confirm_reader("> ") or "").strip()
        if typed != phrase:
            print("Confirmation did not match — aborting. No changes written.", file=sys.stderr)
            return 3

        # Fails closed: re-verify the affected set immediately before writing.
        # Anything that changed the pro_plus population between the dry-run
        # print and this confirmation (a signup, a cancellation, a concurrent
        # run) aborts the whole batch rather than acting on stale data.
        current_plan = build_plan(db)
        current_ids = {p["user_id"] for p in current_plan}
        if len(current_plan) != expected_n or current_ids != confirmed_ids:
            print(
                f"ABORT: affected-user set changed between dry-run ({expected_n}) and "
                f"confirmation ({len(current_plan)}) — no changes written. Re-run to see "
                "the current plan and confirm again.",
                file=sys.stderr,
            )
            return 4

        print(f"\nMigrating {expected_n} user(s)...\n")
        results = run_migration(db, plan)
        migrated = sum(1 for r in results if r["status"] == "migrated")
        skipped = sum(1 for r in results if r["status"].startswith("skipped"))
        failed = sum(1 for r in results if r["status"] == "error")
        print(f"\nDone: {migrated} migrated, {skipped} skipped, {failed} failed.")
        return 0 if failed == 0 else 5
    finally:
        if owns_db:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
