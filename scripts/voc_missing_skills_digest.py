"""scripts/voc_missing_skills_digest.py — topshelf_2605/H.2

Weekly digest of searched-but-missing skill queries.

Queries the top 20 missing queries from the past 7 days and prints a
human-readable digest to stdout.  The actual cron scheduling happens on
Tori separately; this script is standalone (no FastAPI/app context needed
beyond a DATABASE_URL env var).

Usage:
    DATABASE_URL=postgresql://... python scripts/voc_missing_skills_digest.py
    DATABASE_URL=postgresql://... python scripts/voc_missing_skills_digest.py --days 14
    DATABASE_URL=postgresql://... python scripts/voc_missing_skills_digest.py --limit 10
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running from repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main(days: int = 7, limit: int = 20) -> None:
    """Print top ``limit`` missing queries from the past ``days`` days."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    try:
        from sqlalchemy import create_engine, func, text
        from sqlalchemy.orm import sessionmaker
    except ImportError as exc:
        print(f"ERROR: Could not import SQLAlchemy: {exc}", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)

    since = date.today() - timedelta(days=days)

    with Session() as db:
        try:
            rows = (
                db.execute(
                    text(
                        """
                        SELECT lower(query) AS q,
                               SUM(count)   AS total_searches,
                               MIN(day)     AS first_seen,
                               MAX(day)     AS last_seen
                        FROM   missing_skill_queries
                        WHERE  day >= :since
                        GROUP  BY lower(query)
                        ORDER  BY total_searches DESC
                        LIMIT  :limit
                        """
                    ),
                    {"since": since, "limit": limit},
                )
                .fetchall()
            )
        except Exception as exc:
            print(f"ERROR: Query failed: {exc}", file=sys.stderr)
            sys.exit(1)

    if not rows:
        print(f"No missing-skill queries found in the past {days} days.")
        return

    print(f"=== VOC Missing Skills Digest — top {len(rows)} queries (last {days}d) ===")
    print(f"Window: {since} → {date.today()}")
    print()

    header = f"{'#':<4}  {'QUERY':<40}  {'SEARCHES':>8}  {'FIRST SEEN':<12}  {'LAST SEEN':<12}"
    print(header)
    print("-" * len(header))

    for rank, row in enumerate(rows, start=1):
        q, total, first, last = row.q, row.total_searches, row.first_seen, row.last_seen
        print(f"{rank:<4}  {q:<40}  {total:>8}  {str(first):<12}  {str(last):<12}")

    print()
    print(
        f"Action: review above queries and consider adding/improving skills "
        f"that match these unmet needs."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VOC missing-skills weekly digest.")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Look-back window in days (default: 7)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of queries to show (default: 20)",
    )
    args = parser.parse_args()
    main(days=args.days, limit=args.limit)
