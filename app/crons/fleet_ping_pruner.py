"""Phase D — 90-day TTL pruner for fleet_pings.

Run weekly. Drops rows whose `last_seen_day` is more than 90 days behind
today. Idempotent.

Schedule: see `deploy/cron.d` (or systemd timer); manual invocation:
    python -m app.crons.fleet_ping_pruner
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from app.database import SessionLocal
from app.models import FleetPing

logger = logging.getLogger(__name__)

TTL_DAYS = 90


def prune(reference_day: date | None = None) -> int:
    """Delete rows older than `TTL_DAYS`. Returns number of rows removed."""
    cutoff = (reference_day or date.today()) - timedelta(days=TTL_DAYS)
    db = SessionLocal()
    try:
        deleted = (
            db.query(FleetPing)
            .filter(FleetPing.last_seen_day < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        logger.info("fleet_ping_pruner: removed %d rows older than %s", deleted, cutoff)
        return deleted
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    prune()
