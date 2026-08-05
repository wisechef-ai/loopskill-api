#!/usr/bin/env python3
"""scripts/connector_walk.py — mesh_0408 Q-026 connector federation daily walk CLI.

Drives ``app.services.connector_taps.run_daily_walk`` — discovers MCP-server
candidates from the three registered federation taps (docker/mcp-registry,
modelcontextprotocol/servers, the official MCP registry API) and stages the
survivors into the ``ExternalConnector`` table.

WHY THIS EXISTS (mesh_0408 Q-026): ``run_daily_walk`` was proven in
production but had no CLI entrypoint, no in-app caller, and no scheduler —
every staged row carried one single manual-invocation timestamp. This script
closes that gap so a cron can drive the walk daily.

This is a THIN driver. It does not reimplement the walk itself, and it does
not weaken any of the invariants the phase brief established:
  * Staging writes ONLY to ``ExternalConnector`` — never a real
    ``Connector``/``ConnectorVersion`` row (no code path here or in
    connector_taps.py creates one).
  * Every staged row lands ``review_required=True`` — hardcoded in
    ``stage_candidates``, not something this script can influence.
  * The SSRF / dangerous-command guard (``connector_ssrf_guard.py``) runs on
    every candidate BEFORE any insert, in both normal and --dry-run mode.
    This script never adds a bypass flag.

Intended to run daily from cron, mirroring the existing sibling
``federation_reindex`` job (``wisechef`` user crontab on wisechef-hq):
  0 3 * * * cd /home/wisechef/loopskill-api && venv/bin/python scripts/connector_walk.py >> <log> 2>&1

Usage:
  python3 scripts/connector_walk.py             # walk all sources, stage, print a summary
  python3 scripts/connector_walk.py --dry-run   # discover + guard-check only, ZERO DB writes
  python3 scripts/connector_walk.py --json      # machine-readable single summary line

Exit codes (a cron needs these to be meaningful):
  0 = walk succeeded and staged >= 1 row. A ``--dry-run`` also exits 0 on a
      clean report — it never attempts to stage, so the "staged 0" failure
      signal below does not apply to it.
  1 = walk ran but staged 0 rows — every upstream catalog returned nothing,
      or every discovered candidate was blocked by the guard. This is the
      "silently rotting" signal a cron must surface (page/alert on it).
  2 = unhandled error (bad args, DB unavailable, unexpected exception).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("connector_walk")


def _discover_only(_get: Callable[..., Any] | None = None) -> list:
    """Run the three source walkers WITHOUT staging anything. Read-only."""
    from app.services.connector_taps import (
        docker_mcp_registry_walk,
        mcp_servers_walk,
        official_registry_walk,
    )

    candidates = []
    candidates.extend(docker_mcp_registry_walk(_get=_get))
    candidates.extend(mcp_servers_walk(_get=_get))
    candidates.extend(official_registry_walk(_get=_get))
    return candidates


def _guard_report(candidates: list) -> tuple[int, list[str]]:
    """Run the SSRF/dangerous-command guard read-only (no insert) so
    --dry-run can report an honest 'blocked' count without writing anything.
    Reuses the exact same guard function ``stage_candidates`` calls — this
    is not a reimplementation of the guard, just an unwritten dry-run of it.
    """
    from app.services.connector_ssrf_guard import validate_candidate_config

    blocked = 0
    reasons: list[str] = []
    for cand in candidates:
        cand_reasons = validate_candidate_config(cand.config_template)
        if cand_reasons:
            blocked += 1
            reasons.extend(cand_reasons)
    return blocked, reasons


def run_walk(db, *, dry_run: bool = False, as_json: bool = False, _get: Callable[..., Any] | None = None) -> int:
    """Run the connector federation walk (or a dry-run report) and print a
    summary line. Returns the process exit code — see module docstring.
    """
    if dry_run:
        candidates = _discover_only(_get=_get)
        blocked, blocked_reasons = _guard_report(candidates)
        discovered = len(candidates)
        staged = 0
    else:
        from app.services.connector_taps import run_daily_walk

        result = run_daily_walk(db, _get=_get)
        discovered = result.discovered
        staged = result.staged
        blocked = result.blocked
        blocked_reasons = result.blocked_reasons

    summary = {
        "dry_run": dry_run,
        "discovered": discovered,
        "staged": staged,
        "blocked": blocked,
    }

    if as_json:
        print(json.dumps(summary))
    else:
        print(
            "connector_walk: discovered={discovered} staged={staged} blocked={blocked}{suffix}".format(
                discovered=discovered,
                staged=staged,
                blocked=blocked,
                suffix=" (DRY RUN — no writes)" if dry_run else "",
            )
        )

    logger.info(
        "connector_walk complete: discovered=%d staged=%d blocked=%d%s",
        discovered,
        staged,
        blocked,
        " (dry-run)" if dry_run else "",
    )
    if blocked_reasons:
        logger.info(
            "connector_walk: %d blocked reason(s), e.g. %s", len(blocked_reasons), blocked_reasons[:3]
        )

    if dry_run:
        return 0
    return 0 if staged >= 1 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Connector federation daily walk (mesh_0408 Q-026)")
    parser.add_argument("--dry-run", action="store_true", help="discover + guard-check only, no DB writes")
    parser.add_argument("--json", dest="as_json", action="store_true", help="print a single JSON summary line")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return run_walk(db, dry_run=args.dry_run, as_json=args.as_json)
    except Exception:  # noqa: BLE001
        logger.exception("connector_walk: unhandled error")
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
