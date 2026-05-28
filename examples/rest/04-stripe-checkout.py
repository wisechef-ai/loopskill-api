#!/usr/bin/env python3
"""Example 04 — Create a Stripe Checkout session (illustrative).

Demonstrates:
  POST /api/checkout/{tier}

Available tiers: free, pro, pro_plus
Note: this endpoint requires a valid JWT session cookie (set by
/api/auth/github/callback), not just an API key. This example shows
the request shape; browser-based OAuth is required for a real run.

Auth: JWT cookie (set after GitHub OAuth callback) — NOT x-api-key
Env:  RECIPES_BASE_URL — override base URL (default: https://recipes.wisechef.ai)

Usage:
    # Illustrative only — requires a valid session cookie from OAuth:
    python examples/rest/04-stripe-checkout.py --tier pro

    # With a real session cookie (from browser DevTools):
    SESSION_COOKIE=session=xxx python examples/rest/04-stripe-checkout.py --tier pro --real
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BASE_URL = os.environ.get("RECIPES_BASE_URL", "https://recipes.wisechef.ai").rstrip("/")

TIERS = ("free", "pro", "pro_plus")

# Legacy aliases accepted until 2026-06-10 (then retired):
# cook → pro, operator → pro_plus


def create_checkout_session(tier: str, session_cookie: str, dry_run: bool) -> None:
    """POST /api/checkout/{tier} to create a Stripe Checkout Session.

    The server redirects the user to a Stripe-hosted checkout page.
    On success the browser is sent to /api/checkout/success?session_id=...
    On cancel the browser is sent to /api/checkout/cancel.

    The response body contains:
      {
        "checkout_url": "https://checkout.stripe.com/pay/cs_live_...",
        "session_id":   "cs_live_..."
      }
    """
    if tier not in TIERS:
        print(f"Error: tier must be one of {TIERS}", file=sys.stderr)
        sys.exit(1)

    url = f"{BASE_URL}/api/checkout/{tier}"
    print(f"{'[DRY-RUN] Would POST' if dry_run else 'POST'} {url}")
    print(f"  tier: {tier}")

    if dry_run:
        print(
            "\nThis endpoint requires a valid JWT session cookie obtained via:\n"
            f"  1. Navigate to {BASE_URL}/api/auth/github\n"
            "  2. Complete GitHub OAuth flow\n"
            "  3. Copy the 'session' cookie from DevTools → Application → Cookies\n"
            "  4. Set SESSION_COOKIE=session=<value> and re-run with --real\n"
        )
        example_response = {
            "checkout_url": "https://checkout.stripe.com/pay/cs_live_...EXAMPLE...",
            "session_id": "cs_live_...EXAMPLE...",
        }
        print("Example response shape:")
        print(json.dumps(example_response, indent=2))
        return

    if not session_cookie:
        print("Error: SESSION_COOKIE env var is required for --real mode.", file=sys.stderr)
        print("  export SESSION_COOKIE='session=<value from browser>'", file=sys.stderr)
        sys.exit(1)

    req = urllib.request.Request(
        url,
        data=b"",
        headers={
            "Cookie": session_cookie,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())

    print("\nResponse:")
    print(json.dumps(result, indent=2))

    checkout_url = result.get("checkout_url", "")
    if checkout_url:
        print(f"\n→ Open this URL in a browser to complete payment:\n  {checkout_url}")


def check_billing_status(session_cookie: str) -> None:
    """GET /api/billing/me — current user's subscription state."""
    url = f"{BASE_URL}/api/billing/me"
    print(f"\nGET {url}")
    req = urllib.request.Request(
        url,
        headers={"Cookie": session_cookie},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Stripe Checkout session (illustrative)")
    parser.add_argument("--tier", default="pro", choices=list(TIERS), help="Subscription tier")
    parser.add_argument("--real", action="store_true", help="Actually send the request (requires SESSION_COOKIE)")
    parser.add_argument("--billing-status", action="store_true", help="Also call GET /api/billing/me")
    args = parser.parse_args()

    session_cookie = os.environ.get("SESSION_COOKIE", "")
    dry_run = not args.real

    create_checkout_session(args.tier, session_cookie, dry_run=dry_run)

    if args.billing_status and not dry_run:
        check_billing_status(session_cookie)


if __name__ == "__main__":
    main()
