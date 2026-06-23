#!/usr/bin/env python3
"""scripts/federation_reindex.py — superset_0606 Phase B reindex walker.

The background walker that fills the persistent ``federation_index_cache`` table
so a cold ``/api/skills/external`` load reads counts from storage and NEVER
triggers an inline cursor/sitemap walk (decision #7). Registered as the
``recipes-federation-reindex`` cron (daily 03:00, alongside the existing crons).

For each federation source:
  1. Walk its adapter (Phase B uses the existing live search adapters with an
     empty query = full-catalog fetch; Phase D swaps in the deep cursor/sitemap
     walkers for clawhub + skills-sh without changing this driver).
  2. Compute honest indexed-vs-installable counts (decision #5 — installable is
     the route_install-allowed subset, never == indexed by fiat).
  3. Write the row via federation_cache.write_source_cache (which clamps
     installable<=indexed and only advances walked_at on success).
  4. A source that fails its walk records last_error + keeps indexed=NULL so the
     route omits it from the sum rather than fabricating a number.

Bounded + guarded: every fetch already routes through the Phase A SSRF guard;
this driver adds per-source isolation (one bad source never aborts the run) and
a configurable first-page cap so the cache row stays small.

Usage:
  python3 scripts/federation_reindex.py                # walk all sources, write cache
  python3 scripts/federation_reindex.py --source clawhub  # one source
  python3 scripts/federation_reindex.py --dry-run      # walk + report, no DB write
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("federation_reindex")

# First-page cap stored in the cache row (keeps the row small; the page UI only
# shows the first screenful before the user filters/searches).
FIRST_PAGE_CAP = 20
# How many results to pull per source on a full walk. Phase B uses the live
# search adapters' max; Phase D's deep walkers ignore this and walk to
# exhaustion (writing only the count + first page).
WALK_LIMIT = 100


def reindex_source(db, source_id: str, *, dry_run: bool = False) -> dict:
    """Walk one source and (unless dry-run) write its cache row. Returns a report.

    superset_0606 Phase D: if a source has a registered DEEP walker
    (clawhub cursor-walk / skills.sh sitemap-walk), it is preferred over the
    shallow live-search adapter — the deep walk is what produces the giants'
    real ~50k / 20k counts. Sources without a deep walker keep the Phase B
    shallow-adapter full-catalog fetch.
    """
    from app.services import federation_cache as fcache
    from app.services.federation import route_install
    from app.services.federation_adapters import get_adapter
    from app.services.federation_live import LIVE_FETCH
    from app.services.giants_walk import DEEP_WALKERS

    # ── Phase D deep walkers (giants) ────────────────────────────────────
    deep_walker = DEEP_WALKERS.get(source_id)
    if deep_walker is not None:
        try:
            result = deep_walker()
        # Rationale: one giant's walk failure must not abort the whole reindex run.
        except Exception as exc:  # noqa: BLE001
            logger.warning("reindex: deep walk '%s' failed: %s", source_id, exc)
            if not dry_run:
                fcache.write_source_cache(
                    db,
                    source_id,
                    indexed_count=None,
                    installable_count=None,
                    last_error=str(exc)[:500],
                    ttl_seconds=fcache.TTL_DAILY,
                )
            return {"source": source_id, "status": "error", "indexed": None, "error": str(exc)[:120]}

        # A walk that gathered zero AND errored is a failure (NULL → omitted from
        # sum); a walk that gathered rows but hit a late partial error still
        # records the real count it reached (honest partial), with last_error.
        if result.indexed == 0 and result.partial_error:
            if not dry_run:
                fcache.write_source_cache(
                    db,
                    source_id,
                    indexed_count=None,
                    installable_count=None,
                    last_error=result.partial_error[:500],
                    ttl_seconds=fcache.TTL_DAILY,
                )
            return {
                "source": source_id,
                "status": "error",
                "indexed": None,
                "error": result.partial_error[:120],
            }

        if not dry_run:
            fcache.write_source_cache(
                db,
                source_id,
                indexed_count=result.indexed,
                installable_count=result.installable,
                first_page=result.first_page,
                ttl_seconds=fcache.TTL_DAILY,
                last_error=result.partial_error[:500] if result.partial_error else None,
            )
        return {
            "source": source_id,
            "status": "ok",
            "indexed": result.indexed,
            "installable": result.installable,
            "pages": result.pages_walked,
            "exhausted": result.exhausted,
        }

    fetch = LIVE_FETCH.get(source_id)
    adapter = get_adapter(source_id, fetch=fetch)
    if adapter is None:
        return {"source": source_id, "status": "no-adapter", "indexed": None}

    try:
        found = adapter.search("", limit=WALK_LIMIT)  # empty query = full catalog
    # Rationale: one source's walk failure must not abort the whole reindex run.
    except Exception as exc:  # noqa: BLE001
        logger.warning("reindex: source '%s' walk failed: %s", source_id, exc)
        if not dry_run:
            fcache.write_source_cache(
                db,
                source_id,
                indexed_count=None,  # NULL → omitted from sum, never fabricated
                installable_count=None,
                last_error=str(exc)[:500],
                ttl_seconds=fcache.TTL_DAILY,
            )
        return {"source": source_id, "status": "error", "indexed": None, "error": str(exc)[:120]}

    indexed = len(found)
    installable = sum(1 for s in found if route_install(s).allowed)
    first_page = [s.to_dict() for s in found[:FIRST_PAGE_CAP]]

    if not dry_run:
        fcache.write_source_cache(
            db,
            source_id,
            indexed_count=indexed,
            installable_count=installable,
            first_page=first_page,
            ttl_seconds=fcache.TTL_DAILY,
        )
    return {"source": source_id, "status": "ok", "indexed": indexed, "installable": installable}


def main() -> int:
    parser = argparse.ArgumentParser(description="Federation index reindex walker (superset_0606 Phase B)")
    parser.add_argument("--source", help="Walk only this source (default: all live sources)")
    parser.add_argument("--dry-run", action="store_true", help="Walk + report, no DB write")
    args = parser.parse_args()

    from app.database import SessionLocal
    from app.services.federation import LIVE_SOURCES

    sources = [args.source] if args.source else list(LIVE_SOURCES)
    db = SessionLocal()
    reports = []
    try:
        for src in sources:
            report = reindex_source(db, src, dry_run=args.dry_run)
            reports.append(report)
            logger.info(
                "reindex %-16s status=%-8s indexed=%s installable=%s",
                report["source"],
                report["status"],
                report.get("indexed"),
                report.get("installable"),
            )
    finally:
        db.close()

    total_indexed = sum(r["indexed"] for r in reports if isinstance(r.get("indexed"), int))
    ok = sum(1 for r in reports if r["status"] == "ok")
    logger.info(
        "reindex complete: %d/%d sources OK, total indexed=%s%s",
        ok,
        len(reports),
        total_indexed,
        " (DRY RUN — no writes)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
