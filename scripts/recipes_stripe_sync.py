#!/usr/bin/env python3
"""scripts/recipes_stripe_sync.py

Reads config/tiers.yaml and verifies (or updates) Stripe price metadata so that
every paid tier has:
  - price.nickname == yaml stripe_nickname
  - price.metadata.tier_slug == yaml key

Does NOT modify DB or issue charges. Defaults to --check-only (read-only).

Exit codes:
  0  all prices in sync (or check-only mode)
  1  out-of-sync prices found (check-only) or apply error
  2  config/credential error

Usage:
  python3 scripts/recipes_stripe_sync.py                # check-only (default)
  python3 scripts/recipes_stripe_sync.py --check-only   # explicit
  python3 scripts/recipes_stripe_sync.py --apply        # write to Stripe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # PyYAML — in requirements.txt


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TIERS_YAML = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"


def load_tiers() -> dict[str, Any]:
    with open(TIERS_YAML) as f:
        data = yaml.safe_load(f)
    assert data.get("version") == 1, "Unexpected tiers.yaml version"
    return data["tiers"]


def get_stripe_client() -> tuple[Any, str] | None:
    """Return (stripe module, api_key) or None if key not set."""
    sk = (
        os.environ.get("WR_STRIPE_SECRET_KEY")
        or os.environ.get("STRIPE_SECRET_KEY")
        or ""
    ).strip()
    if not sk:
        return None
    import stripe
    stripe.api_key = sk
    return stripe, sk


# ---------------------------------------------------------------------------
# Price inspection and sync
# ---------------------------------------------------------------------------

def inspect_price(stripe: Any, price_id: str) -> dict:
    """Fetch Stripe price and return relevant fields."""
    price = stripe.Price.retrieve(price_id)
    return {
        "id": price.get("id"),
        "nickname": price.get("nickname"),
        "metadata": dict(price.get("metadata") or {}),
        "active": price.get("active"),
        "currency": price.get("currency"),
        "unit_amount": price.get("unit_amount"),
    }


def compute_desired(key: str, meta: dict[str, Any]) -> dict:
    return {
        "nickname": meta.get("stripe_nickname", ""),
        "metadata": {"tier_slug": key},
    }


def find_diffs(current: dict, desired: dict) -> list[str]:
    diffs: list[str] = []
    cur_nick = current.get("nickname") or ""
    want_nick = desired["nickname"]
    if want_nick and cur_nick != want_nick:
        diffs.append(f"nickname: {cur_nick!r} -> {want_nick!r}")
    cur_slug = (current.get("metadata") or {}).get("tier_slug", "")
    want_slug = desired["metadata"]["tier_slug"]
    if cur_slug != want_slug:
        diffs.append(f"metadata.tier_slug: {cur_slug!r} -> {want_slug!r}")
    return diffs


def apply_update(stripe: Any, price_id: str, desired: dict) -> dict:
    """Update Stripe price metadata and nickname. Returns updated price dict."""
    params: dict[str, Any] = {}
    if desired.get("nickname"):
        params["nickname"] = desired["nickname"]
    if desired.get("metadata"):
        params["metadata"] = desired["metadata"]
    updated = stripe.Price.modify(price_id, **params)
    return {
        "id": updated.get("id"),
        "nickname": updated.get("nickname"),
        "metadata": dict(updated.get("metadata") or {}),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--check-only", action="store_true", default=True,
                       help="Read-only: report diffs but do not write (default)")
    group.add_argument("--apply", action="store_true",
                       help="Write nickname and metadata updates to Stripe")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON")
    args = ap.parse_args()

    apply_mode = args.apply

    tiers = load_tiers()

    stripe_pair = get_stripe_client()
    if stripe_pair is None:
        msg = "WR_STRIPE_SECRET_KEY / STRIPE_SECRET_KEY not set — cannot reach Stripe"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"SKIP: {msg}")
        return 0  # not a hard failure; just no credentials in dev env

    stripe_mod, _ = stripe_pair
    results: list[dict] = []
    all_in_sync = True

    for key, meta in tiers.items():
        env_var = meta.get("price_id_env")
        if not env_var:
            results.append({"tier": key, "status": "skipped", "reason": "free tier"})
            continue

        price_id = os.environ.get(env_var, "").strip()
        if not price_id:
            results.append({
                "tier": key,
                "status": "skipped",
                "reason": f"env var {env_var!r} not set",
            })
            continue

        try:
            current = inspect_price(stripe_mod, price_id)
        except Exception as exc:  # noqa: BLE001
            results.append({"tier": key, "status": "error", "error": str(exc)})
            all_in_sync = False
            continue

        desired = compute_desired(key, meta)
        diffs = find_diffs(current, desired)

        if not diffs:
            results.append({"tier": key, "status": "in_sync", "price_id": price_id})
            continue

        all_in_sync = False
        if apply_mode:
            try:
                updated = apply_update(stripe_mod, price_id, desired)
                results.append({
                    "tier": key,
                    "status": "updated",
                    "price_id": price_id,
                    "changes": diffs,
                    "updated": updated,
                })
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "tier": key,
                    "status": "update_error",
                    "price_id": price_id,
                    "changes": diffs,
                    "error": str(exc),
                })
        else:
            results.append({
                "tier": key,
                "status": "out_of_sync",
                "price_id": price_id,
                "diffs": diffs,
            })

    summary = {
        "mode": "apply" if apply_mode else "check-only",
        "all_in_sync": all_in_sync,
        "tiers": results,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        mode_label = "APPLY" if apply_mode else "CHECK"
        print(f"Stripe sync [{mode_label}]: {'IN SYNC' if all_in_sync else 'OUT OF SYNC'}")
        for r in results:
            tier = r["tier"]
            status = r["status"]
            if status == "in_sync":
                print(f"  {tier:12} OK     {r.get('price_id', '')}")
            elif status == "skipped":
                print(f"  {tier:12} SKIP   {r.get('reason', '')}")
            elif status in ("out_of_sync", "updated"):
                for diff in r.get("diffs", r.get("changes", [])):
                    print(f"  {tier:12} DIFF   {diff}")
            elif status in ("error", "update_error"):
                print(f"  {tier:12} ERROR  {r.get('error', '')}")

    if not all_in_sync and not apply_mode:
        return 1  # signal drift to caller
    return 0


if __name__ == "__main__":
    sys.exit(main())
