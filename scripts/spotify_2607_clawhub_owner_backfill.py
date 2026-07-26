"""scripts/spotify_2607_clawhub_owner_backfill.py — repair 69,150 dead deep links.

Issue #141. ClawHub skill pages are owner-scoped (``/<owner>/skills/<slug>``);
we stored the bare ``/skills/<slug>`` form for every ClawHub row. ClawHub 307s
that to ``/skills/skills/<slug>``, a client-rendered soft-404 that still answers
**HTTP 200** — so no status-code probe ever caught it. ClawHub is
``install_path=deep_link`` by policy, so the link IS the deliverable: a broken
one makes the row worthless, not degraded.

PR #140 fixed the MINT path. It could not repair rows already in the table.
This script does that.

STRATEGY (why it is not 69,150 sequential calls)
------------------------------------------------
The plan projected one detail-API call per slug. Probing upstream on 2026-07-26
found a much cheaper path:

  * ``GET /api/search?q=<term>&limit=500`` returns rows that ALREADY carry
    ``ownerHandle``. Eight seed terms produced 3,744 distinct pairs in 39s.
  * ``GET /v1/feeds/skills`` is an official, robots-allowed bulk feed.
  * Any single query saturates at ~1,026 distinct results regardless of
    ``limit``, so breadth comes from many terms, not one huge page.

So: bulk-harvest first, then spend per-slug detail calls only on the remainder.
``--max-detail-calls`` bounds that tail so a run cannot silently turn into a
multi-hour hammering of someone else's API.

SAFETY POSTURE
--------------
* **Dry-run by default.** ``--commit`` to write.
* **Idempotent + resumable.** Only touches rows still lacking ``owner_handle``.
  Interrupt it, re-run it, it converges.
* **Partial state is VALID.** An unresolved row keeps the browse-page fallback,
  which is a working link. We never invent a handle — a guessed deep link 404s
  confidently, which is worse than an honest fallback.
* **Every handle is re-validated** through ``is_safe_token`` before it can reach
  a URL or the database.

DURABILITY
----------
``bulk_upsert_skills`` DELETES every row on each nightly reindex (03:00 cron)
and the snapshot carries no owner field. Backfilling alone would therefore be
undone within 24 hours. ``hub_snapshot.load_resolved_owner_handles`` +
``apply_resolved_owners`` carry the resolution across that delete — this script
is only half the fix, and the carry-forward is the half that makes it last.

USAGE
-----
    python3 scripts/spotify_2607_clawhub_owner_backfill.py             # dry run
    python3 scripts/spotify_2607_clawhub_owner_backfill.py --commit
    python3 scripts/spotify_2607_clawhub_owner_backfill.py --commit \
        --max-detail-calls 5000 --batch-size 500
"""
from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("clawhub_owner_backfill")


def get_db_url() -> str:
    """Resolve the database URL from env, else alembic.ini."""
    url = os.environ.get("WR_DATABASE_URL")
    if url:
        return url
    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "alembic.ini")
    return cfg["alembic"]["sqlalchemy.url"]


#: Durable checkpoint for the resolved owner map.
#:
#: LEARNED THE HARD WAY 2026-07-26: an earlier measurement run cached the map in
#: ``/tmp`` and a host reboot wiped it, discarding ~78 minutes of upstream API
#: work. ``/tmp`` is cleared on boot on this host — never put expensive,
#: slow-to-recompute state there.
#:
#: The database is still the real source of resumability (``owner_handle`` is
#: persisted per row, so a re-run only queries rows that are still NULL). This
#: file is a second, cheaper layer: it lets a DRY RUN — which writes nothing to
#: the DB by definition — resume instead of re-sweeping from scratch, and it
#: means an interrupted --commit run does not re-pay for slugs it already
#: resolved but had not yet flushed.
DEFAULT_CACHE_PATH = Path.home() / ".hermes" / "state" / "clawhub-owner-map.json"


def load_cache(path: Path) -> dict[str, str]:
    """Load a previously-checkpointed owner map. Never fatal."""
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    # Rationale: the cache is an optimisation, not a source of truth — a corrupt
    # or unreadable file must degrade to "start fresh", never abort the backfill.
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner-map cache unreadable at %s (%s) — starting fresh", path, exc)
    return {}


def save_cache(path: Path, owner_map: dict[str, str]) -> None:
    """Checkpoint the owner map atomically. Never fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a truncated file
        # that the next run would read as an empty/partial map.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(owner_map))
        tmp.replace(path)
        logger.info("checkpointed %d owner pairs -> %s", len(owner_map), path)
    # Rationale: checkpointing is best-effort; a read-only or full disk must not
    # destroy an otherwise-successful backfill run.
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not checkpoint owner map to %s: %s", path, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="write changes (default: dry run)")
    parser.add_argument(
        "--max-detail-calls",
        type=int,
        default=2000,
        help="cap per-slug detail lookups for rows the bulk sweep missed (default 2000)",
    )
    parser.add_argument("--batch-size", type=int, default=500, help="rows per DB flush (default 500)")
    parser.add_argument("--skip-feed", action="store_true", help="skip the verified-publisher feed")
    parser.add_argument(
        "--skip-targeted",
        action="store_true",
        help="skip the identifier-derived targeted sweep (stage 2). Not recommended: "
        "the generic seed list alone covers only ~20%% of real prod rows.",
    )
    parser.add_argument(
        "--max-terms",
        type=int,
        default=1500,
        help="term budget for the targeted sweep (default 1500, ~3.5s each)",
    )
    parser.add_argument(
        "--skip-tail",
        action="store_true",
        help="skip the parallel per-slug tail (stage 3). Coverage stops at whatever "
        "the sweeps reached (~71%% measured); remaining rows get the browse fallback.",
    )
    parser.add_argument(
        "--tail-workers",
        type=int,
        default=10,
        help="concurrency for the tail (default 10, hard cap 16). Measured ~20x "
        "faster than serial; kept modest because this is a third-party API.",
    )
    parser.add_argument(
        "--tail-chunk",
        type=int,
        default=2000,
        help="checkpoint the owner map every N tail slugs (default 2000, ~1 min of "
        "work). Bounds how much upstream effort a crash can discard.",
    )
    parser.add_argument(
        "--cache-path",
        default=str(DEFAULT_CACHE_PATH),
        help=f"durable owner-map checkpoint (default {DEFAULT_CACHE_PATH}). "
        "NEVER put this in /tmp — it is wiped on reboot.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore and do not write the checkpoint; re-sweep everything from scratch.",
    )
    parser.add_argument(
        "--limit-rows",
        type=int,
        default=0,
        help="only process the first N unresolved rows (0 = all). For rehearsal.",
    )
    args = parser.parse_args()

    os.environ.setdefault("WR_DATABASE_URL", get_db_url())

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models import FederationHubSkill
    from app.services.clawhub_owner_bulk import (
        build_owner_map,
        resolve_owner_via_detail,
        resolve_tail_parallel,
        targeted_owner_sweep,
    )
    from app.services.clawhub_url import clawhub_skill_url, is_safe_token

    engine = create_engine(get_db_url())

    with Session(engine) as db:
        stmt = select(FederationHubSkill).where(
            FederationHubSkill.upstream_source == "clawhub",
            FederationHubSkill.owner_handle.is_(None),
        )
        if args.limit_rows:
            stmt = stmt.limit(args.limit_rows)
        rows = list(db.execute(stmt).scalars())

        total_clawhub = db.execute(
            select(FederationHubSkill.id).where(FederationHubSkill.upstream_source == "clawhub")
        ).all()

        logger.info(
            "clawhub rows: %d total, %d still unresolved (this run)",
            len(total_clawhub),
            len(rows),
        )
        if not rows:
            logger.info("nothing to do — every clawhub row already has an owner_handle")
            return 0

        wanted = {(r.identifier or "").strip() for r in rows if (r.identifier or "").strip()}

        # Durable checkpoint: resume instead of re-paying for upstream calls we
        # have already made. See DEFAULT_CACHE_PATH — a /tmp cache cost ~78 min
        # of API work to a host reboot on 2026-07-26.
        cache_path = Path(args.cache_path).expanduser()
        owner_map: dict[str, str] = {} if args.no_cache else load_cache(cache_path)
        if owner_map:
            pre = sum(1 for w in wanted if w in owner_map)
            logger.info(
                "resumed %d cached pairs from %s (covers %d/%d wanted, %.1f%%)",
                len(owner_map), cache_path, pre, len(wanted), pre / max(len(wanted), 1) * 100,
            )

        # ── 1. Generic bulk harvest ───────────────────────────────────────
        def _progress(term: str, added: int, cumulative: int) -> None:
            logger.info("  seed  term=%-10s +%-4d unique=%d", term, added, cumulative)

        if sum(1 for w in wanted if w in owner_map) >= len(wanted):
            logger.info("stage 1/4: SKIPPED — cache already covers every wanted slug")
        else:
            logger.info("stage 1/4: generic bulk map (feed=%s)...", not args.skip_feed)
            owner_map.update(build_owner_map(use_feed=not args.skip_feed, progress=_progress))
            if not args.no_cache:
                save_cache(cache_path, owner_map)
        covered = sum(1 for w in wanted if w in owner_map)
        logger.info(
            "stage 1 done: %d pairs, covering %d/%d wanted (%.1f%%)",
            len(owner_map), covered, len(wanted), covered / max(len(wanted), 1) * 100,
        )

        # ── 2. Targeted sweep on what is still missing ────────────────────
        # Measured: the generic seed list alone covers only ~20% of real prod
        # identifiers, because broad terms saturate on the same popular results.
        # Terms derived from the unresolved slugs themselves resolve ~300 rows
        # per call vs 1 per detail call.
        if not args.skip_targeted:
            def _tprogress(term: str, added: int, total: int, remaining: int) -> None:
                if added or remaining % 5000 == 0:
                    logger.info(
                        "  target term=%-16s +%-4d map=%-6d remaining=%d",
                        term, added, total, remaining,
                    )

            logger.info("stage 2/4: targeted sweep (max %d terms)...", args.max_terms)
            owner_map = targeted_owner_sweep(
                wanted=wanted,
                known=owner_map,
                max_terms=args.max_terms,
                progress=_tprogress,
            )
            if not args.no_cache:
                save_cache(cache_path, owner_map)
            covered = sum(1 for w in wanted if w in owner_map)
            logger.info(
                "stage 2 done: %d pairs, covering %d/%d wanted (%.1f%%)",
                len(owner_map), covered, len(wanted), covered / max(len(wanted), 1) * 100,
            )

        # ── 3. Parallel tail: resolve what the sweeps could not ───────────
        # Measured: once the large same-prefix families are exhausted, both
        # exact-slug search and the detail endpoint yield ~1.0 resolutions per
        # call — the endpoint is not the bottleneck, serialisation is. At 10
        # workers a ~20k tail takes ~35 min instead of ~22 h.
        if not args.skip_tail:
            still = sorted(w for w in wanted if w not in owner_map)
            if still:
                logger.info(
                    "stage 3/4: parallel tail (%d slugs, %d workers, chunk %d)...",
                    len(still), args.tail_workers, args.tail_chunk,
                )
                # Chunked so an interruption costs MINUTES of upstream work, not
                # the whole tail. The 2026-07-26 reboot killed a single 78-minute
                # in-memory run and discarded all of it.
                for start in range(0, len(still), args.tail_chunk):
                    chunk = still[start : start + args.tail_chunk]
                    owner_map.update(
                        resolve_tail_parallel(chunk, workers=args.tail_workers)
                    )
                    if not args.no_cache:
                        save_cache(cache_path, owner_map)
                    done = min(start + len(chunk), len(still))
                    cov = sum(1 for w in wanted if w in owner_map)
                    logger.info(
                        "  tail %d/%d  coverage %d/%d (%.1f%%)",
                        done, len(still), cov, len(wanted), cov / max(len(wanted), 1) * 100,
                    )
                covered = sum(1 for w in wanted if w in owner_map)
                logger.info(
                    "stage 3 done: covering %d/%d wanted (%.1f%%)",
                    covered, len(wanted), covered / max(len(wanted), 1) * 100,
                )

        # ── 4. Apply ──────────────────────────────────────────────────────
        logger.info("stage 4/4: applying (detail-call cap %d)...", args.max_detail_calls)
        resolved = 0
        detail_calls = 0
        detail_hits = 0
        unresolved = 0
        demoted = 0

        for row in rows:
            identifier = (row.identifier or "").strip()
            owner = owner_map.get(identifier) if identifier else None

            if identifier and owner is None and detail_calls < args.max_detail_calls:
                detail_calls += 1
                owner = resolve_owner_via_detail(identifier)
                if owner:
                    detail_hits += 1
                    owner_map[identifier] = owner  # memoise for duplicate identifiers

            if owner and is_safe_token(owner):
                row.owner_handle = owner
                row.origin_url = clawhub_skill_url(identifier, owner)
                resolved += 1
            else:
                unresolved += 1
                # CRITICAL: an unresolved row must NOT keep the bare
                # `/skills/<slug>` form. That is the confirmed soft-404 (307 ->
                # /skills/skills/<slug>) this whole issue is about — leaving it
                # in place would keep advertising a dead link AND would make the
                # acceptance gate ("zero rows match ^https://clawhub\.ai/skills/")
                # permanently unreachable.
                #
                # Demote it to the browse page instead: less precise, but it
                # actually renders. `clawhub_skill_url` with no owner returns
                # exactly that, and the browse URL has no trailing slash so it
                # does not match the gate's regex.
                #
                # owner_handle stays NULL, so the next run retries this row —
                # the demotion is a safe floor, not a terminal state.
                fallback = clawhub_skill_url(identifier or None, None)
                if row.origin_url != fallback:
                    row.origin_url = fallback
                    demoted += 1

            if args.commit and (resolved + demoted) % args.batch_size == 0:
                db.flush()
                db.commit()
                logger.info(
                    "  committed %d resolved / %d demoted (detail calls: %d)",
                    resolved, demoted, detail_calls,
                )

        if args.commit:
            db.commit()

        # Final checkpoint: detail-call resolutions discovered during apply are
        # the most expensive pairs in the map (one upstream call each) — losing
        # them to a crash would be the worst possible trade.
        if not args.no_cache:
            save_cache(cache_path, owner_map)

        logger.info("─" * 60)
        logger.info("resolved   : %d / %d (%.1f%%)  -> owner-scoped deep link", resolved, len(rows), resolved / max(len(rows), 1) * 100)
        logger.info("  from sweeps: %d", resolved - detail_hits)
        logger.info("  from detail: %d (%d calls, cap %d)", detail_hits, detail_calls, args.max_detail_calls)
        logger.info("unresolved : %d  -> browse-page fallback (a WORKING link)", unresolved)
        logger.info("  demoted off the soft-404 bare form this run: %d", demoted)
        logger.info("mode       : %s", "COMMITTED" if args.commit else "DRY RUN (no writes)")
        if unresolved:
            logger.info("re-run to resolve more; the script is resumable and idempotent")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
