"""feat/member-loop-apply — CLI: pull assignments, apply to local Hermes crons.

Usage (from the member host's sync cron, after the skills reconcile step):

    RECIPES_API_KEY=<member-key> python -m app.loop_apply_cli \
        --api https://app.loopskill.io \
        --jobs-file ~/.hermes/cron/jobs.json [--dry-run]

Prints one JSON document: {"status": ..., "created": [...], "updated": [...],
"removed": [...], "skipped": [...]}. Exit 0 on success (including no-change),
1 on any error — mirrors app.reconcile_cli conventions so loopskill-sync.sh
can treat both identically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from app.loop_apply import apply_assignments


def _fetch_assignments(api: str, key: str) -> dict:
    req = urllib.request.Request(
        f"{api.rstrip('/')}/api/my/loop-assignments",
        headers={"x-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # Rationale: fixed https API base from CLI arg, member-key auth.
        return json.loads(resp.read().decode())


def main(argv: list[str] | None = None) -> int:
    """Entry point: fetch the member's assignments and reconcile local crons."""
    ap = argparse.ArgumentParser(description="Apply LoopSkill loop assignments to local Hermes crons")
    ap.add_argument("--api", default="https://app.loopskill.io")
    ap.add_argument("--jobs-file", default=os.path.expanduser("~/.hermes/cron/jobs.json"))
    ap.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    args = ap.parse_args(argv)

    key = os.environ.get("RECIPES_API_KEY", "")
    if not key:
        print(json.dumps({"status": "error", "error": "RECIPES_API_KEY not set"}))
        return 1

    try:
        payload = _fetch_assignments(args.api, key)
    # Rationale: network/auth failure must emit a parseable error doc, not a traceback.
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": str(e)[:300]}))
        return 1

    assignments = payload.get("assignments", [])
    jobs_path = Path(args.jobs_file)

    if args.dry_run:
        # Apply against a throwaway copy so the diff is real but nothing lands.
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_jobs = Path(td) / "jobs.json"
            if jobs_path.exists():
                shutil.copy(jobs_path, tmp_jobs)
            result = apply_assignments(assignments, tmp_jobs)
        out = {"status": "dry_run", **result.to_dict()}
    else:
        result = apply_assignments(assignments, jobs_path)
        out = {"status": "applied" if result.changed else "up_to_date", **result.to_dict()}

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
