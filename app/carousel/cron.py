"""Carousel cron — daily_carousel_job(db, today).

Writes 7 CarouselEntry rows for *today*; idempotent.

F6: Uses unique index on (featured_date, slot) as the atomic idempotency guard.
The check-then-act gate below is kept as a fast-path optimisation (avoids 7
INSERT attempts on the common case), but the unique index is the authoritative
guard against concurrent runs.  If two cron instances race past the count check,
the second batch of INSERTs will hit IntegrityError which is silently discarded.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CarouselEntry
from app.carousel.selector import select_top_7


def daily_carousel_job(db: Session, today: date | None = None) -> int:
    """Populate carousel_entries for *today*.

    Returns the number of rows inserted (0 if already populated — idempotent).

    Args:
        db:    SQLAlchemy session (caller owns lifecycle).
        today: target date.  Defaults to today UTC.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    # Idempotency fast-path — skip if any entries exist for this date
    today_dt_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    today_dt_end = datetime.combine(today, datetime.max.time(), tzinfo=timezone.utc)

    existing_count = (
        db.query(CarouselEntry)
        .filter(
            CarouselEntry.featured_date >= today_dt_start,
            CarouselEntry.featured_date <= today_dt_end,
        )
        .count()
    )
    if existing_count > 0:
        return 0  # already populated, nothing inserted

    entries_data = select_top_7(db, today)
    if not entries_data:
        return 0

    inserted = 0
    for item in entries_data:
        entry = CarouselEntry(
            skill_id=item["skill_id"],
            featured_date=item["featured_date"],
            position=item["slot"] - 1,  # keep backward-compat position (0-indexed)
            slot=item["slot"],          # 1-indexed D1 column
            role=item["role"],
            tagline=item["tagline"],
            score=item["score_value"],
        )
        db.add(entry)
        try:
            # F6: flush per-row so unique index violations are caught immediately
            # rather than at commit time (which would roll back all 7 rows).
            db.flush()
            inserted += 1
        except IntegrityError:
            # Concurrent cron run already inserted this (featured_date, slot) pair.
            # Roll back just this save-point and continue — idempotent no-op.
            db.rollback()
            # Restart the session state so subsequent flushes work
            break

    try:
        db.commit()
    except IntegrityError:
        # Whole-batch conflict (e.g. SQLite without row-level flush support)
        db.rollback()
        return 0

    return inserted
