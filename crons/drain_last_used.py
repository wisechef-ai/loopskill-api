"""Drain cron — bulk-flush last_used_at from Redis/memory to the database.

Run periodically (e.g. every 2 minutes) via cron or systemd timer to
bulk-UPDATE api_keys.last_used_at for all keys that have been used since
the last drain.  This replaces the per-request DB commit introduced in
the original middleware (Issue #17 fix: write amplification).

Usage:
    python -m crons.drain_last_used
    # or directly:
    python crons/drain_last_used.py
"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wiserecipes.drain_last_used")


def main() -> None:
    """Drain last_used_at timestamps from Redis/memory to the database."""
    from app.database import SessionLocal
    from app.last_used_tracker import tracker
    from app.middleware import get_redis

    # Wire up Redis if available (tracker may have started in memory-only mode).
    redis_client = get_redis()
    if redis_client is not None and tracker.redis is None:
        tracker.redis = redis_client

    db = SessionLocal()
    try:
        n = tracker.drain(db)
        logger.info("drain_last_used: updated %d api key(s)", n)
    except Exception:  # noqa: BLE001 — Rationale: top-level cron; log-and-exit
        logger.exception("drain_last_used: unhandled error during drain")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
