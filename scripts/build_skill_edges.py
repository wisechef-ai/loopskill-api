#!/usr/bin/env python3
"""Build derived skill-graph edges and persist them to the DB.

Run from the API host:

    cd /home/wisechef/recipes-api
    .venv/bin/python scripts/build_skill_edges.py [--dry-run]

Prints a JSON summary for cron scraping. Exits non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from app.database import SessionLocal
from app.edge_builder import build_edges, persist_edges, WEIGHT_THRESHOLD


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute but do not write")
    args = ap.parse_args()

    db = SessionLocal()
    started = datetime.now(timezone.utc)
    try:
        edges = build_edges(db)
        wrote = 0 if args.dry_run else persist_edges(db, edges)
        if not args.dry_run:
            db.commit()

        summary = {
            "ok": True,
            "dry_run": args.dry_run,
            "edge_count": len(edges),
            "rows_persisted": wrote,
            "weight_threshold": WEIGHT_THRESHOLD,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(summary, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }, indent=2), file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
