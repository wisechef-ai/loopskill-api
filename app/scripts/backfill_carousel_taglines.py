"""pick_1605 Phase C — backfill carousel tagline from skill.description.

Symptom (validated 2026-05-16 on prod):
  GET /api/carousel/today entries have `tagline == skill.title` for every row.
  The selector code already does `description[:80] if description else title[:80]`,
  but existing CarouselEntry rows were written by older code that wrote the title.

Fix:
  For each CarouselEntry whose featured_date is in [target_start, target_end],
  re-derive tagline from the linked Skill:
    tagline = (skill.description or skill.title)[:80].rstrip()
  Skip rows where the current tagline is already a description-prefix (idempotent).

Usage:
    .venv/bin/python -m app.scripts.backfill_carousel_taglines           # today (UTC)
    .venv/bin/python -m app.scripts.backfill_carousel_taglines 7         # last 7 days
    .venv/bin/python -m app.scripts.backfill_carousel_taglines --dry-run # show diff only

Exits 0 on success, prints a one-line summary plus per-row diff to stdout.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from app.database import SessionLocal
from app.models import CarouselEntry, Skill


def derive_tagline(skill: Skill) -> str:
    """Mirror the selector logic exactly — keep these in sync."""
    description = skill.description or ""
    if description:
        return description[:120]
    return (skill.title or "")[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill carousel taglines from skill.description.")
    parser.add_argument(
        "days",
        nargs="?",
        type=int,
        default=1,
        help="Number of days back from today (UTC) to backfill (default: 1 = today only).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print proposed changes without committing.")
    args = parser.parse_args()

    end_dt = datetime.now(UTC).replace(hour=23, minute=59, second=59, microsecond=999999)
    start_dt = (end_dt - timedelta(days=args.days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    db = SessionLocal()
    try:
        entries = (
            db.query(CarouselEntry)
            .join(Skill, CarouselEntry.skill_id == Skill.id)
            .filter(
                CarouselEntry.featured_date >= start_dt,
                CarouselEntry.featured_date <= end_dt,
            )
            .all()
        )

        if not entries:
            print(f"No carousel entries in {start_dt.date()}..{end_dt.date()} — nothing to backfill.")
            return 0

        changed = 0
        unchanged = 0
        for e in entries:
            new_tagline = derive_tagline(e.skill)
            old_tagline = e.tagline or ""
            if old_tagline == new_tagline:
                unchanged += 1
                continue
            print(f"  {e.featured_date.date()} slot={e.slot} {e.skill.slug}:")
            print(f"    OLD: {old_tagline!r}")
            print(f"    NEW: {new_tagline!r}")
            if not args.dry_run:
                e.tagline = new_tagline
            changed += 1

        if args.dry_run:
            print(f"\n[DRY-RUN] would update {changed} rows; {unchanged} already correct.")
        else:
            db.commit()
            print(f"\nBackfilled {changed} carousel taglines; {unchanged} already correct.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
