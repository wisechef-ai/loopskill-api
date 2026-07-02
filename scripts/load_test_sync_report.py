#!/usr/bin/env python
"""§B load test for POST /api/sync-report — Phase T (activate_0701).

Measures p95 latency of the batched sync-report endpoint at 10x pro-max rate.

Usage:
    python scripts/load_test_sync_report.py --base-url http://localhost:8000 \\
        --member-key rec_live_xxx --fleet-id <uuid> --duration 30

The test sends one POST per ~1.25 seconds (the 10x pro-max rate is ~480
POSTs/day/member; we approximate the sustained rate). At scale this would
be 96,000 POSTs/day equivalent (200 agents × 480 POSTs/day).

For local testing against a running uvicorn + SQLite instance:
    uvicorn app.main:create_app --factory --port 8000 &
    python scripts/load_test_sync_report.py --base-url http://localhost:8000 \\
        --member-key <key> --duration 10

VPS-class extrapolation (documented in docs/design/):
    At 200 agents with 48 POSTs/day each (default 30-min cycle = 48 cycles/day),
    that's 9,600 POSTs/day. At 10x pro-max (aggressive 1.25s cadence), 96,000.
    Each POST carries ~200 loop_runs + ~50 skill_errors + cron_health.
    DB rows/day at 200 agents: ~9,600 × 250 = 2.4M raw rows, ~9,600 rollup rows.
    Pruned at 30d retention → steady-state ~72M raw rows, ~288K rollup rows.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime


def _make_payload(n_loop_runs: int = 200, n_skill_errors: int = 50) -> dict:
    """Build a representative batched payload at the D9 cap."""
    now_iso = datetime.now(UTC).isoformat()
    return {
        "cycle_ts": now_iso,
        "loop_runs": [
            {
                "loop_slug": f"loop-{i % 10}",
                "instance_key": "agent/default",
                "outcome": "success" if i % 5 != 0 else "failure",
                "accepted_change": i % 7 == 0,
                "cost_usd": round(0.01 * (i % 100), 2),
                "duration_seconds": 60 + (i % 300),
                "provenance_id": f"prov-{i}",
                "started_at": now_iso,
                "detail": f"run detail {i}"[:200],
            }
            for i in range(n_loop_runs)
        ],
        "skill_errors": [
            {
                "slug": f"skill-{i % 5}",
                "semver": "1.0.0",
                "signature": f"sig-{i}",
                "summary": f"error summary {i}"[:200],
            }
            for i in range(n_skill_errors)
        ],
        "cron_health": {
            "failed": [
                {"job_name": f"cron-{i}", "last_status": "error", "consecutive_failures": i} for i in range(5)
            ],
            "counts": {"total": 48, "ok": 43, "error": 5},
        },
    }


def _post(base_url: str, member_key: str, payload: dict) -> float:
    """POST the payload and return latency in ms."""
    url = f"{base_url.rstrip('/')}/api/sync-report"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": member_key,
        },
        method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        print(f"  HTTP {e.code}: {e.reason} ({elapsed_ms:.0f}ms)", file=sys.stderr)
        return elapsed_ms
    elapsed_ms = (time.monotonic() - start) * 1000
    return elapsed_ms


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test /api/sync-report")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--member-key", required=False, help="Member API key (rec_live_...)")
    parser.add_argument("--duration", type=int, default=30, help="Test duration in seconds")
    parser.add_argument("--interval", type=float, default=1.25, help="Seconds between POSTs")
    parser.add_argument("--loop-runs", type=int, default=200, help="loop_runs per POST")
    parser.add_argument("--skill-errors", type=int, default=50, help="skill_errors per POST")
    args = parser.parse_args()

    if not args.member_key:
        print("ERROR: --member-key is required", file=sys.stderr)
        return 1

    print(f"Load test: POST /api/sync-report for {args.duration}s")
    print(f"  Base URL: {args.base_url}")
    print(f"  Interval: {args.interval}s per POST")
    print(f"  Payload: {args.loop_runs} loop_runs, {args.skill_errors} skill_errors")
    print()

    payload = _make_payload(args.loop_runs, args.skill_errors)
    latencies: list[float] = []
    errors = 0

    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        try:
            lat = _post(args.base_url, args.member_key, payload)
            latencies.append(lat)
        except Exception as e:  # noqa: BLE001
            print(f"  Error: {e}", file=sys.stderr)
            errors += 1
        time.sleep(args.interval)

    if not latencies:
        print("No successful requests!", file=sys.stderr)
        return 1

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[min(int(len(latencies) * 0.99), len(latencies) - 1)]

    print(f"Results ({len(latencies)} requests, {errors} errors):")
    print(f"  Mean:  {statistics.mean(latencies):.1f}ms")
    print(f"  p50:   {p50:.1f}ms")
    print(f"  p95:   {p95:.1f}ms")
    print(f"  p99:   {p99:.1f}ms")
    print(f"  Min:   {min(latencies):.1f}ms")
    print(f"  Max:   {max(latencies):.1f}ms")
    print()

    if p95 < 200:
        print(f"✓ PASS: p95 ({p95:.1f}ms) < 200ms")
        return 0
    else:
        print(f"✗ FAIL: p95 ({p95:.1f}ms) >= 200ms")
        return 1


if __name__ == "__main__":
    sys.exit(main())
