#!/usr/bin/env python3
"""scripts/connector_promote.py — conn_promote_0821 quality-gated staged->listed
connector promotion CLI.

Drives ``app.services.connector_promote.run_promotion_pass`` — evaluates
staged ``ExternalConnector`` rows against the deterministic gate set
(license allow-list, structural/secret-lint validation, SSRF re-check,
name/description sanity, dup-slug, reachability probe) and, with ``--apply``,
mints real ``Connector``/``ConnectorVersion`` rows for every candidate that
passes ALL gates. A row that fails stays ``review_required=True`` with its
failure reason(s) recorded on the row.

Mirrors ``scripts/connector_walk.py``'s CLI shape and cron-safety discipline
(thin driver, dry-run default, no bypass flag for the guards) and
``scripts/bundle_validate.py``'s gate/exit-code convention.

WHY THIS EXISTS: the daily walk (``connector_walk.py``) stages candidates but
by design NEVER promotes — see its own module docstring. With 2,400+ rows
staged and zero promoted, there was no code path from "staged" to "listed"
at all. This script is that path, gated so nothing low-quality or
unreachable ever reaches the public catalog automatically.

Usage:
  python3 scripts/connector_promote.py                 # dry-run (default), all eligible rows
  python3 scripts/connector_promote.py --apply          # evaluate AND write promotions
  python3 scripts/connector_promote.py --limit 100      # cap how many rows this pass evaluates
  python3 scripts/connector_promote.py --json           # machine-readable single summary line

Exit codes (matches the connector_walk.py convention):
  0 = ran and promoted >= 1 row. A dry-run exits 0 when >= 1 row WOULD pass
      every gate (the closest analog it has, same anti-rot logic as
      connector_walk.py's --dry-run treating "discovered 0" as exit 1).
  1 = ran cleanly but promoted 0 rows (dry-run: 0 rows would pass). This is
      the "the gate backlog isn't moving" signal a cron should page on —
      distinct from "no eligible rows left" only in the human-readable
      summary, since both are legitimately exit 1 (nothing to promote).
  2 = unhandled error (bad args, DB unavailable, or any other unexpected
      exception anywhere in main()).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("connector_promote")


def run(db, *, apply: bool, limit: int | None, as_json: bool, _head=None) -> int:
    """Run one promotion pass and print a summary line. Returns the process
    exit code — see module docstring for the exact contract."""
    from app.services.connector_promote import run_promotion_pass

    results, outcome = run_promotion_pass(db, apply=apply, limit=limit, _head=_head)

    would_pass = sum(1 for r in results if r.passed)
    failed = [r for r in results if not r.passed and not r.transient]
    deferred = [r for r in results if r.transient]

    summary = {
        "apply": apply,
        "evaluated": len(results),
        "would_pass": would_pass,
        "failed": len(failed),
        "deferred": len(deferred),
    }
    if outcome is not None:
        summary.update(
            {
                "promoted": outcome.promoted,
                "rejected": outcome.rejected,
                "already_promoted": outcome.already_promoted,
                "outcome_deferred": outcome.deferred,
            }
        )

    if as_json:
        print(json.dumps(summary))
    else:
        mode = "APPLY" if apply else "DRY RUN — no writes"
        print(
            "connector_promote ({mode}): evaluated={evaluated} would_pass={would_pass} "
            "failed={failed} deferred={deferred}".format(mode=mode, **summary)
        )
        if outcome is not None:
            print(
                f"  applied: promoted={outcome.promoted} rejected={outcome.rejected} "
                f"already_promoted={outcome.already_promoted} deferred={outcome.deferred}"
            )
        if failed:
            logger.info("connector_promote: %d rejected row(s), e.g.:", len(failed))
            for r in failed[:5]:
                print(f"    ✗ {r.slug}: {'; '.join(r.reasons)}")
        if deferred:
            logger.info("connector_promote: %d deferred (transient) row(s)", len(deferred))

    logger.info(
        "connector_promote complete: evaluated=%d would_pass=%d failed=%d deferred=%d%s",
        len(results),
        would_pass,
        len(failed),
        len(deferred),
        f" applied(promoted={outcome.promoted})" if outcome is not None else " (dry-run)",
    )

    promoted_count = outcome.promoted if outcome is not None else would_pass
    return 0 if promoted_count >= 1 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quality-gated staged->listed connector promotion (conn_promote_0821)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="write promotions (default is dry-run, zero writes)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="cap how many eligible rows this pass evaluates"
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true", help="print a single JSON summary line"
    )
    args = parser.parse_args()

    db = None
    try:
        from app.database import SessionLocal

        db = SessionLocal()
        return run(db, apply=args.apply, limit=args.limit, as_json=args.as_json)
    except Exception:  # noqa: BLE001 — Rationale: any unhandled error here (bad
        # args already handled by argparse, DB unreachable, unexpected exception
        # inside run()) is an infra error, not a "nothing to promote" signal.
        logger.exception("connector_promote: unhandled error")
        return 2
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 — Rationale: a close failure must
                # never mask the exit code already determined above.
                logger.exception("connector_promote: error closing db session")


if __name__ == "__main__":
    raise SystemExit(main())
