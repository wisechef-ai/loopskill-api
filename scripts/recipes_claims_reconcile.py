#!/usr/bin/env python3
"""scripts/recipes_claims_reconcile.py

Reads config/tiers.yaml, queries DB tier distribution, queries Stripe price
metadata, and reports any drift between layers.

Layers compared:
  1. YAML  — config/tiers.yaml (SSOT)
  2. DB    — distribution of tier column in user/subscription table
  3. Stripe — price.nickname and price.metadata.tier_slug per paid tier

Exit codes:
  0  clean (all layers agree with yaml)
  1  drift detected (only in --strict mode)

Usage:
  python3 scripts/recipes_claims_reconcile.py --check-only   # print diffs, always exit 0
  python3 scripts/recipes_claims_reconcile.py --strict       # exit 1 on any drift
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow running from repo root without installing the package.
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


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def query_db_tier_distribution() -> dict[str, int] | None:
    """Return {tier_slug: count} from DB, or None if DB unavailable."""
    db_url = (
        os.environ.get("WR_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    )
    if not db_url:
        return None
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(db_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            # rev 7.2 hotfix: column is 'subscription_tier' on the users table,
            # not 'tier' (Skill.tier exists; User.subscription_tier is the user-facing one).
            rows = conn.execute(
                text("SELECT subscription_tier AS tier, COUNT(*) AS cnt "
                     "FROM users GROUP BY subscription_tier")
            ).fetchall()
        return {row[0]: int(row[1]) for row in rows if row[0]}
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}


# ---------------------------------------------------------------------------
# Stripe layer
# ---------------------------------------------------------------------------

def query_stripe_prices(tiers: dict[str, Any]) -> dict[str, dict] | None:
    """Return {tier_key: {nickname, tier_slug_meta}} from Stripe, or None."""
    sk = (
        os.environ.get("WR_STRIPE_SECRET_KEY")
        or os.environ.get("STRIPE_SECRET_KEY")
        or ""
    )
    if not sk:
        return None
    try:
        import stripe
        stripe.api_key = sk
        result: dict[str, dict] = {}
        for key, meta in tiers.items():
            env_var = meta.get("price_id_env")
            if not env_var:
                continue  # free tier — no price
            price_id = os.environ.get(env_var, "")
            if not price_id:
                result[key] = {"__error__": f"env var {env_var} not set"}
                continue
            try:
                price = stripe.Price.retrieve(price_id)
                # rev 7.2 hotfix: stripe SDK v15 returns Price objects, not dicts.
                # `.get()` works on a SubResource but not on the top-level Price obj.
                # Use attribute access with getattr() fallback for safety.
                meta_obj = getattr(price, "metadata", None) or {}
                tier_slug_meta = (
                    meta_obj.get("tier_slug") if hasattr(meta_obj, "get") else None
                )
                result[key] = {
                    "nickname": getattr(price, "nickname", None),
                    "tier_slug_meta": tier_slug_meta,
                    "price_id": price_id,
                }
            except Exception as exc:  # noqa: BLE001
                result[key] = {"__error__": str(exc)}
        return result
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def check_db_drift(tiers: dict[str, Any], db_dist: dict[str, int]) -> list[str]:
    issues: list[str] = []
    if "__error__" in db_dist:
        issues.append(f"DB query error: {db_dist['__error__']}")
        return issues
    yaml_slugs = {meta["db_slug"] for meta in tiers.values()}
    db_slugs = set(db_dist.keys())
    unknown = db_slugs - yaml_slugs
    for slug in sorted(unknown):
        issues.append(
            f"DB has tier={slug!r} (count={db_dist[slug]}) not present in tiers.yaml"
        )
    return issues


def check_stripe_drift(tiers: dict[str, Any], stripe_prices: dict[str, dict]) -> list[str]:
    issues: list[str] = []
    if "__error__" in stripe_prices:
        issues.append(f"Stripe query error: {stripe_prices['__error__']}")
        return issues
    for key, meta in tiers.items():
        if not meta.get("price_id_env"):
            continue
        if key not in stripe_prices:
            continue
        info = stripe_prices[key]
        if "__error__" in info:
            issues.append(f"Stripe price lookup error for {key!r}: {info['__error__']}")
            continue
        want_nick = meta.get("stripe_nickname", "")
        got_nick = info.get("nickname") or ""
        if want_nick and got_nick != want_nick:
            issues.append(
                f"Stripe price {info['price_id']!r} nickname mismatch for {key!r}: "
                f"yaml={want_nick!r} stripe={got_nick!r}"
            )
        got_slug = info.get("tier_slug_meta") or ""
        if got_slug and got_slug != key:
            issues.append(
                f"Stripe price metadata.tier_slug mismatch for {key!r}: "
                f"yaml_key={key!r} stripe_meta={got_slug!r}"
            )
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="Exit 1 if any drift is detected")
    ap.add_argument("--check-only", action="store_true",
                    help="Print diffs without exiting non-zero (overrides --strict)")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON")
    args = ap.parse_args()

    tiers = load_tiers()
    report: dict[str, Any] = {"yaml_tiers": list(tiers.keys()), "issues": []}

    # DB layer
    db_dist = query_db_tier_distribution()
    if db_dist is None:
        report["db"] = "skipped (WR_DATABASE_URL/DATABASE_URL not set)"
    else:
        report["db_distribution"] = db_dist
        report["issues"].extend(check_db_drift(tiers, db_dist))

    # Stripe layer
    stripe_prices = query_stripe_prices(tiers)
    if stripe_prices is None:
        report["stripe"] = "skipped (WR_STRIPE_SECRET_KEY/STRIPE_SECRET_KEY not set)"
    else:
        report["stripe_prices"] = stripe_prices
        report["issues"].extend(check_stripe_drift(tiers, stripe_prices))

    drift = len(report["issues"]) > 0
    report["drift"] = drift
    report["result"] = "DRIFT" if drift else "CLEAN"

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Tiers SSOT reconcile: {report['result']}")
        print(f"  YAML tiers : {', '.join(report['yaml_tiers'])}")
        if "db_distribution" in report:
            dist_str = ", ".join(f"{k}={v}" for k, v in report["db_distribution"].items())
            print(f"  DB dist    : {dist_str or '(empty)'}")
        else:
            print(f"  DB         : {report.get('db', 'skipped')}")
        if "stripe_prices" in report:
            for k, v in report["stripe_prices"].items():
                if "__error__" in v:
                    print(f"  Stripe {k:10}: ERROR — {v['__error__']}")
                else:
                    print(f"  Stripe {k:10}: nickname={v.get('nickname')!r} tier_slug_meta={v.get('tier_slug_meta')!r}")
        else:
            print(f"  Stripe     : {report.get('stripe', 'skipped')}")
        for issue in report["issues"]:
            print(f"  DRIFT: {issue}")

    if drift and args.strict and not args.check_only:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
