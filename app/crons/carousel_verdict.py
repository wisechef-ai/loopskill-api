"""Day-7 verdict cron — judge skills that exited the carousel 7 days ago.

Verdict logic:
  - Compare a skill's recent install velocity vs the median for its category.
  - PROMOTE: velocity >= 1.5x median → flag in skills.editorial_score (column added later)
  - HOLD: velocity 0.6-1.5x median → no action, eligible to re-enter rotation in 90d
  - ARCHIVE: velocity < 0.6x median → mark skills.is_public = false (soft-archive)

For now: log the decisions only — wiring the actual writes is a follow-up
once we've observed real telemetry on the live carousel.
"""
from __future__ import annotations
import os, sys, datetime as dt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

DATABASE_URL = os.environ.get("WR_DATABASE_URL", settings.DATABASE_URL)


def main():
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        target_date = (dt.datetime.utcnow() - dt.timedelta(days=7)).date()
        rows = session.execute(text("""
            SELECT s.id, s.slug, s.title, s.category,
                   COUNT(DISTINCT ie.id) FILTER (WHERE ie.created_at > :cutoff) AS recent_installs
              FROM carousel_entries ce
              JOIN skills s ON s.id = ce.skill_id
         LEFT JOIN install_events ie ON ie.skill_id = s.id
             WHERE ce.featured_date = :d
             GROUP BY s.id
        """), {"d": target_date, "cutoff": dt.datetime.utcnow() - dt.timedelta(days=7)}).all()

        if not rows:
            print(f"[verdict] no carousel entries for {target_date}, nothing to judge")
            return

        # Compute median velocity per category
        by_cat: dict[str, list[int]] = {}
        for r in rows:
            by_cat.setdefault(r.category or "uncategorized", []).append(int(r.recent_installs or 0))
        medians = {c: sorted(v)[len(v) // 2] for c, v in by_cat.items()}

        promotions = holds = archives = 0
        for r in rows:
            cat = r.category or "uncategorized"
            median = max(1, medians.get(cat, 1))
            ratio = (r.recent_installs or 0) / median
            if ratio >= 1.5:
                verdict = "PROMOTE"
                promotions += 1
            elif ratio < 0.6:
                verdict = "ARCHIVE"
                archives += 1
            else:
                verdict = "HOLD"
                holds += 1
            print(f"[verdict] {r.slug:30s} cat={cat:14s} v={r.recent_installs} "
                  f"(med {median}) ratio={ratio:.2f} → {verdict}")

        print(f"[verdict] {target_date}: {promotions} promote, {holds} hold, {archives} archive")
    finally:
        session.close()


if __name__ == "__main__":
    main()
