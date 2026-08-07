#!/usr/bin/env python3
"""W2 RED-proof harness — mutate each fix, confirm the RIGHT test goes red.

A check that has never been seen to fail is decoration, not evidence (trap V1).
And a verification harness can itself be broken (trap V4), so every mutation
asserts that it actually changed bytes before running anything.

Usage: python3 scripts/_w2_redproof.py
Exits non-zero if any mutation fails to turn its guard test red, or if any
mutation is a no-op, or if the tree is not restored clean afterwards.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (label, file, find, replace, test selector, the property being proven)
MUTATIONS = [
    (
        "phantom-MRR",
        "app/subscription_service.py",
        "    return figures_for(sub)",
        '    return RevenueFigures(\n        real_usd=cents_to_usd(995), list_usd=cents_to_usd(995), discount_pct=None\n    )',
        "tests/test_w2_revenue_alert_truth.py",
        "a revenue alert cannot report list price as realised revenue",
    ),
    (
        "entitlement-status-gate",
        "app/revenue_truth.py",
        '    if getattr(user, "subscription_status", None) not in HEALTHY_SUB_STATUSES:\n        return None',
        "    pass  # MUTATED: status check removed",
        "tests/test_w2_dunning_entitlement.py",
        "a failed renewal loses entitlement (past_due != entitled)",
    ),
    (
        "webhook-ordering-guard",
        "app/subscription_service.py",
        "    return event_ts < prior",
        "    return False  # MUTATED: staleness guard disabled",
        "tests/test_w2_dunning_entitlement.py",
        "an older Stripe event cannot clobber newer subscription state",
    ),
    (
        "payment-failed-revocation",
        "app/subscription_service.py",
        '    user.subscription_status = "past_due"\n    _record_event_ts(user, event_ts)',
        "    _record_event_ts(user, event_ts)  # MUTATED: no past_due",
        "tests/test_w2_dunning_entitlement.py",
        "invoice.payment_failed revokes entitlement",
    ),
    (
        "synthetic-separability",
        "app/billable_units.py",
        '        (func.coalesce(APIKey.is_test, False).is_(True), "synthetic"),',
        '        (func.coalesce(APIKey.is_test, False).is_(None), "synthetic"),  # MUTATED',
        "tests/test_w2_billable_units.py",
        "synthetic (self-beacon) traffic is separable from billable usage",
    ),
    (
        "coupon-array-shape",
        "app/revenue_truth.py",
        '    many = subscription.get("discounts")\n    if isinstance(many, list):\n        raw.extend(many)',
        "    many = None  # MUTATED: `discounts` array ignored",
        "tests/test_w2_revenue_truth.py",
        "both Stripe coupon shapes are read (array-only coupon not missed)",
    ),
    (
        "exact-discount-pct",
        "app/revenue_alerts.py",
        "    elif discount_pct is not None:\n        label = f\"{_pct(discount_pct)}% off\"",
        '    elif discount_pct is not None:\n        label = f"{_pct((Decimal(1) - (real_usd / list_usd)) * Decimal(100))}% off"',
        "tests/test_w2_revenue_alert_truth.py",
        "the discount % is computed from exact cents, not rounded dollars",
    ),
    (
        "list-price-ssot",
        "app/admin_routes.py",
        "        list_ceiling += tier_list_monthly_usd(canonical)",
        '        list_ceiling += Decimal({"pro": 20, "pro_plus": 100}.get(canonical, 0))  # MUTATED',
        "tests/test_pulse_endpoint.py",
        "the pulse list ceiling comes from the config/tiers.yaml SSOT",
    ),
]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def git_clean() -> bool:
    return run(["git", "status", "--porcelain"]).stdout.strip() == ""


def main() -> int:
    if not git_clean():
        print("REFUSING: working tree is dirty — commit first so restore is verifiable.")
        return 2

    failures: list[str] = []
    for label, rel, find, replace, selector, prop in MUTATIONS:
        path = ROOT / rel
        original = path.read_text()
        mutated = original.replace(find, replace, 1)

        # Trap V4: a mutation that changes no bytes must raise, never pass.
        if mutated == original:
            failures.append(f"{label}: ANCHOR MOVED — mutation changed nothing in {rel}")
            print(f"  ✗ {label}: anchor not found in {rel}")
            continue

        path.write_text(mutated)
        try:
            result = run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", selector])
            went_red = result.returncode != 0
            first_fail = next(
                (line for line in result.stdout.splitlines() if line.startswith("FAILED")),
                "",
            )
        finally:
            path.write_text(original)

        if went_red:
            print(f"  ✓ {label}: RED as required — {prop}")
            if first_fail:
                print(f"      {first_fail}")
        else:
            failures.append(f"{label}: STAYED GREEN with the fix removed — {prop}")
            print(f"  ✗ {label}: stayed GREEN — the test proves nothing")

    if not git_clean():
        failures.append("tree not restored clean after mutations")
        print("  ✗ git status is dirty after restore")
    else:
        print("  ✓ tree restored clean (git status --porcelain empty)")

    if failures:
        print("\nRED-PROOF FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nRED-PROOF PASSED: {len(MUTATIONS)}/{len(MUTATIONS)} mutations went red.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
