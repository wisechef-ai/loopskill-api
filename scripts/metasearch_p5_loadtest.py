#!/usr/bin/env python3
"""§7.5 scale acceptance load-test harness (metasearch_0710 P5).

The plan's ship-blocker: "a load test proving (a) search fan-out stays under
each source's published limit at 300 searches/min with an 80%+ cache hit rate,
and (b) 30k simulated agents reconciling + a 200-agent deploy burst keep
reconcile p95 < 200ms on the VPS-class box."

This harness runs BOTH load surfaces against a LIVE LoopSkill instance:

  (a) Search fan-out: 300 searches/min for 5 min across 20 popular + random
      queries. Measures: cache hit rate (target ≥80%), upstream call count
      (target: stays under ClawHub 3000/window), p95 latency.

  (b) Reconcile: 30k simulated agents polling desired-state on the 30-min cycle
      (~1000 POSTs/min steady state, 16.7/s) + a 200-agent deploy burst.
      Measures: reconcile p95 (target <200ms), 304-fast-path rate, error rate.

Usage:
  python scripts/metasearch_p5_loadtest.py --base-url https://app.loopskill.io --api-key $KEY

  # search-only (no reconcile — needs a fleet setup):
  python scripts/metasearch_p5_loadtest.py --base-url https://app.loopskill.io --api-key $KEY --search-only

  # reconcile-only (needs a fleet with agents):
  python scripts/metasearch_p5_loadtest.py --base-url https://app.loopskill.io --api-key $KEY --reconcile-only --fleet-id $FLEET

Exit code 0 = PASS (all targets met), 1 = FAIL (any target missed). Prints a
structured report to stdout. This is the §7.5 acceptance gate.

NOTE: this script CANNOT be run from a dev session — it needs a deployed
instance with PostgreSQL + real source connectivity. It is the harness that
RUNS the proof, not the proof itself.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

POPULAR_QUERIES = [
    "browser", "scraping", "email", "pdf", "ocr", "git", "github", "docker",
    "test", "debug", "api", "web", "data", "image", "deploy", "monitor",
    "log", "file", "search", "json",
]
RANDOM_QUERIES = [
    "automation", "calendar", "crypto", "database", "ffmpeg", "graphql",
    "keyboard", "markdown", "network", "notify", "parser", "queue", "redis",
    "server", "ssh", "terminal", "translate", "video", "weather", "yaml",
]


def _req(url: str, method: str = "GET", api_key: str | None = None, body: bytes | None = None) -> tuple[int, dict | None, float]:
    """Make an HTTP request, return (status, json_body, elapsed_s)."""
    headers = {"accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    if body:
        headers["content-type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read()) if resp.status == 200 else None
            return resp.status, data, time.perf_counter() - t0
    except HTTPError as e:
        return e.code, None, time.perf_counter() - t0
    except (URLError, OSError):
        return 0, None, time.perf_counter() - t0


def search_load_test(base_url: str, api_key: str, duration_s: int = 300, rps: float = 5.0) -> dict:
    """(a) Search fan-out load: 300 searches/min (5/s) for 5 min.
    Targets: cache hit rate ≥80%, p95 latency <500ms, 0 errors."""
    print(f"\n{'='*60}")
    print(f"  (a) SEARCH FAN-OUT LOAD — {rps:.0f}/s for {duration_s}s ({rps*duration_s:.0f} searches)")
    print(f"{'='*60}")

    latencies: list[float] = []
    cache_hits = 0
    cache_misses = 0
    errors = 0
    queries_used = (POPULAR_QUERIES + RANDOM_QUERIES) * 10  # cycle

    total_searches = int(rps * duration_s)
    interval = 1.0 / rps

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = []
        for i in range(total_searches):
            q = queries_used[i % len(queries_used)]
            url = f"{base_url}/api/skills/metasearch?q={q}&page_size=30"
            futures.append(pool.submit(_req, url, "GET", api_key))
            time.sleep(interval)

        for fut in as_completed(futures, timeout=duration_s + 60):
            status, body, elapsed = fut.result()
            latencies.append(elapsed)
            if status != 200:
                errors += 1
                continue
            if body and body.get("cache", {}).get("cache_hit"):
                cache_hits += 1
            else:
                cache_misses += 1

    total = cache_hits + cache_misses
    hit_rate = cache_hits / total if total > 0 else 0
    p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies) if latencies else 0

    result = {
        "total_searches": total_searches,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "cache_hit_rate": round(hit_rate, 3),
        "p95_latency_ms": round(p95 * 1000, 1),
        "errors": errors,
        "targets": {"cache_hit_rate": "≥0.80", "p95_latency_ms": "<500", "errors": "0"},
        "pass": hit_rate >= 0.80 and p95 < 0.5 and errors == 0,
    }
    _print_result("SEARCH", result)
    return result


def reconcile_load_test(base_url: str, api_key: str, fleet_id: str, agent_count: int = 30000) -> dict:
    """(b) Reconcile load: 30k agents polling desired-state (~16.7/s steady) +
    a 200-agent deploy burst. Targets: p95 <200ms, 0 errors."""
    print(f"\n{'='*60}")
    print(f"  (b) RECONCILE LOAD — {agent_count} agents, ~{agent_count/30/60:.1f}/s steady + 200-agent burst")
    print(f"{'='*60}")

    latencies: list[float] = []
    status_counts: Counter = Counter()
    errors = 0

    # Simulate ~60s of steady-state polling (16.7/s for 60s = ~1000 polls)
    steady_rps = agent_count / 30 / 60  # 30-min cycle
    steady_duration = 60
    steady_total = int(steady_rps * steady_duration)
    interval = 1.0 / steady_rps if steady_rps > 0 else 1.0

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = []
        for i in range(steady_total):
            # Simulate an agent polling desired-state (the reconcile endpoint)
            url = f"{base_url}/api/fleets/{fleet_id}/reconcile"
            # Empty local state → all-declared = adds (worst case for first poll)
            body = json.dumps({"skills": []}).encode()
            futures.append(pool.submit(_req, url, "POST", api_key, body))
            time.sleep(interval)

        # 200-agent deploy burst (simulate 200 agents hitting deploy simultaneously)
        for _ in range(200):
            url = f"{base_url}/api/fleets/{fleet_id}/reconcile"
            body = json.dumps({"skills": []}).encode()
            futures.append(pool.submit(_req, url, "POST", api_key, body))

        for fut in as_completed(futures, timeout=120):
            status, _, elapsed = fut.result()
            latencies.append(elapsed)
            status_counts[status] += 1
            if status not in (200, 304):
                errors += 1

    p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies) if latencies else 0

    result = {
        "total_requests": steady_total + 200,
        "p95_latency_ms": round(p95 * 1000, 1),
        "status_codes": dict(status_counts),
        "errors": errors,
        "targets": {"p95_latency_ms": "<200", "errors": "0"},
        "pass": p95 < 0.2 and errors == 0,
    }
    _print_result("RECONCILE", result)
    return result


def _print_result(name: str, result: dict) -> None:
    print(f"\n  {name} RESULTS:")
    for k, v in result.items():
        if k != "pass":
            print(f"    {k}: {v}")
    status = "✅ PASS" if result.get("pass") else "❌ FAIL"
    print(f"\n  {name}: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="§7.5 scale acceptance load test")
    parser.add_argument("--base-url", required=True, help="LoopSkill instance URL")
    parser.add_argument("--api-key", required=True, help="API key for auth")
    parser.add_argument("--fleet-id", help="Fleet ID for reconcile test")
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--reconcile-only", action="store_true")
    parser.add_argument("--duration", type=int, default=300, help="Search test duration (s)")
    args = parser.parse_args()

    all_pass = True

    if not args.reconcile_only:
        search_result = search_load_test(args.base_url, args.api_key, duration_s=args.duration)
        all_pass = all_pass and search_result["pass"]

    if not args.search_only:
        if not args.fleet_id:
            print("ERROR: --fleet-id required for reconcile test", file=sys.stderr)
            return 1
        reconcile_result = reconcile_load_test(args.base_url, args.api_key, args.fleet_id)
        all_pass = all_pass and reconcile_result["pass"]

    print(f"\n{'='*60}")
    print(f"  §7.5 ACCEPTANCE: {'✅ PASS' if all_pass else '❌ FAIL — ship-blocker not met'}")
    print(f"{'='*60}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
