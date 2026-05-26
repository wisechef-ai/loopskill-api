#!/usr/bin/env python3
"""Bulk-close stale drift-probe noise issues on wisechef-ai/recipes-api.

Phase B of repohygiene_2605: closes zero-comment agent-reported recipe:bug
issues opened by app/github-actions. Filter predicate is intentionally strict
to avoid touching any real user-filed issue.

Usage:
    python scripts/bulk_close_drift_probe_noise.py --dry-run
    python scripts/bulk_close_drift_probe_noise.py --confirm
    python scripts/bulk_close_drift_probe_noise.py --confirm --max 10
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

REPO = "wisechef-ai/recipes-api"
AUTHOR = "app/github-actions"
LABELS = ["agent-reported", "recipe:bug"]

RATIONALE_TEMPLATE = """\
Closed by repohygiene_2605 Phase B (bulk noise drain).

**Why:** the drift-probe-cron at scripts/install_count_drift_probe.py was opening \
a new issue every hour for the same (skill, drift) pair because Python's builtin \
hash() is salt-randomized per process (PYTHONHASHSEED defaults to random), so \
error_signature drifted on every cron tick and the dispatcher had no dedup match.

**Fix:** signature stability fix landed in 63ffff7 (PR #303) — deterministic \
hashlib.sha256 over a canonical input + per-(slug, signature) 24h rate limit in \
the probe itself. Live verified: two probe runs 60s apart now produce 0 net-new \
GitHub issues.

**What this means for you:** if the underlying skill still fails on a fresh \
install, a single (stably-deduped) issue will be opened by the next probe cycle. \
The 9 hot skills (larry, multi-agent-discord-coordination, pr-draft, \
clean-architecture, client-reporter, code-review, incident-response, \
domain-driven-design, graphify) are tracked in #{follow_up} for root-cause fixes.

If this issue is closed in error, please reopen — the bulk-close predicate \
requires zero human comments AND author=app/github-actions, so a real \
user-reported issue should not have hit this filter. Apologies for the noise.\
"""


def fetch_issues(limit: int = 200) -> list[dict]:
    """Fetch open issues matching the noise filter via gh CLI."""
    label_args = []
    for lbl in LABELS:
        label_args += ["--label", lbl]

    cmd = [
        "gh", "issue", "list",
        "--repo", REPO,
        "--state", "open",
        "--author", AUTHOR,
        "--limit", str(limit),
        "--json", "number,title,createdAt,comments",
    ] + label_args

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: gh issue list failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    return json.loads(result.stdout)


def close_issue(number: int, rationale: str) -> bool:
    """Close a single issue with a rationale comment. Returns True on success."""
    cmd = [
        "gh", "issue", "close", str(number),
        "--repo", REPO,
        "--comment", rationale,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"ERROR: failed to close #{number}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Bulk-close drift-probe noise issues (repohygiene_2605/B)"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="List issues that WOULD be closed; no side-effects.",
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help="Actually close the issues.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=200,
        metavar="N",
        help="Cap the number of issues closed (default: 200).",
    )
    parser.add_argument(
        "--follow-up",
        type=int,
        default=312,
        metavar="ISSUE",
        help="Follow-up tracking issue number (default: 312).",
    )
    args = parser.parse_args()

    rationale = RATIONALE_TEMPLATE.format(follow_up=args.follow_up)

    print(f"Fetching open issues from {REPO} ...")
    print(f"  Filter: author={AUTHOR!r}, labels={LABELS}")
    issues = fetch_issues(limit=max(args.max, 200))
    print(f"  Fetched {len(issues)} candidate issues.")

    closed = 0
    skipped_comments = 0
    errors = 0

    for issue in issues:
        if closed >= args.max:
            print(f"  Reached --max {args.max}; stopping.")
            break

        num = issue["number"]
        title = issue["title"]
        n_comments = len(issue.get("comments", []))

        if n_comments > 0:
            print(f"  SKIP  #{num} ({n_comments} comment(s)): {title}")
            skipped_comments += 1
            continue

        if args.dry_run:
            print(f"  DRY   #{num}: {title}")
            closed += 1  # count "would close" for summary
        else:
            print(f"  CLOSE #{num}: {title}")
            ok = close_issue(num, rationale)
            if ok:
                closed += 1
            else:
                errors += 1
            time.sleep(0.4)  # avoid GitHub secondary rate limits

    total_scanned = len(issues)
    print()
    print("=" * 60)
    if args.dry_run:
        print("DRY-RUN summary:")
        print(f"  would close:              {closed}")
        print(f"  skipped (has comments):   {skipped_comments}")
        print(f"  total scanned:            {total_scanned}")
    else:
        print("LIVE summary:")
        print(f"  closed:                   {closed}")
        print(f"  skipped (has comments):   {skipped_comments}")
        print(f"  errors:                   {errors}")
        print(f"  total scanned:            {total_scanned}")
    print("=" * 60)

    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
