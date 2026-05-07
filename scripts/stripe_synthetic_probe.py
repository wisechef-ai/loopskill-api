#!/usr/bin/env python3
"""scripts/stripe_synthetic_probe.py

Read-only synthetic probe for Stripe live integration. Wired into
~/.hermes/scripts/audit-to-paperclip.py (or daily-agent-audit equivalent)
to run on Sundays so it doesn't double-up with the daily probes.

What it checks:
1. STRIPE_SECRET_KEY is set and starts with sk_live_ or sk_test_
2. stripe.Balance.retrieve() succeeds (proves API key works, no charge)
3. stripe.Webhook.construct_event() can verify a self-signed test event
   (proves the SDK and webhook secret pattern haven't drifted)
4. STRIPE_WEBHOOK_SECRET is set and starts with whsec_

Does NOT issue any charges, refunds, or modifications. Pure read + signature
verification.

Exit codes:
  0 — all green
  1 — credentials missing
  2 — Stripe API call failed
  3 — webhook signature verify failed (SDK regression)

Usage:
  python3 scripts/stripe_synthetic_probe.py
  python3 scripts/stripe_synthetic_probe.py --json   # machine-readable output
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from importlib.metadata import version as _pkg_version


def _check_credentials() -> tuple[str, str]:
    sk = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    whsec = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not sk or not (sk.startswith("sk_live_") or sk.startswith("sk_test_")):
        print("ERR: STRIPE_SECRET_KEY missing or wrong prefix", file=sys.stderr)
        sys.exit(1)
    if not whsec or not whsec.startswith("whsec_"):
        print("ERR: STRIPE_WEBHOOK_SECRET missing or wrong prefix", file=sys.stderr)
        sys.exit(1)
    return sk, whsec


def _check_stripe_api(sk: str) -> dict:
    import stripe
    stripe.api_key = sk
    try:
        bal = stripe.Balance.retrieve()
        # bal is a stripe object; convert to dict for portable output
        bal_dict = bal.to_dict() if hasattr(bal, "to_dict") else dict(bal)
        return {
            "ok": True,
            "object": bal_dict.get("object"),
            "livemode": bal_dict.get("livemode"),
            "currencies": [
                a.get("currency") for a in (bal_dict.get("available") or [])
            ],
        }
    except Exception as exc:  # noqa: BLE001
        print(f"ERR: stripe.Balance.retrieve() failed: {exc}", file=sys.stderr)
        sys.exit(2)


def _check_webhook_signature(whsec: str) -> dict:
    """Self-signed test event — proves construct_event works against this SDK
    + webhook secret pair. Catches the SDK 15.x regression class without
    needing a live event from Stripe.
    """
    import stripe
    payload = json.dumps(
        {
            "id": "evt_synthetic_probe",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_synthetic"}},
        },
        separators=(",", ":"),
    ).encode()
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload
    secret = whsec.encode()
    sig = hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()
    sig_header = f"t={timestamp},v1={sig}"

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, whsec)
        return {
            "ok": True,
            "type": event.get("type") if isinstance(event, dict) else event["type"],
            "id": event.get("id") if isinstance(event, dict) else event["id"],
        }
    except Exception as exc:  # noqa: BLE001
        print(f"ERR: webhook signature verify failed: {exc}", file=sys.stderr)
        sys.exit(3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON (single line) instead of human-readable text",
    )
    args = parser.parse_args()

    sk, whsec = _check_credentials()
    sdk_version = _pkg_version("stripe")
    api_result = _check_stripe_api(sk)
    webhook_result = _check_webhook_signature(whsec)

    summary = {
        "stripe_sdk": sdk_version,
        "secret_key_prefix": sk[:8] + "***",
        "webhook_secret_prefix": whsec[:6] + "***",
        "api_call": api_result,
        "webhook_verify": webhook_result,
        "result": "PASS",
    }

    if args.json:
        print(json.dumps(summary))
    else:
        print(f"✓ Stripe synthetic probe PASS")
        print(f"  SDK version: {sdk_version}")
        print(f"  API call:    {api_result['object']} (livemode={api_result['livemode']})")
        print(f"  Webhook:     {webhook_result['type']} ({webhook_result['id']}) signature OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
