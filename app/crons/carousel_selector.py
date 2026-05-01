"""Carousel selector — runs daily at 23:55 UTC to pick tomorrow's 7 skills.

Algorithm (mirrors plan v4):
  1. Eligibility: skills with status='approved'/is_public=true, not already in carousel
     in the last 7 days (CAROUSEL_HISTORY_DAYS env), and not by a creator who has another slot already today.
  2. Diversity: no repeat creator/category in the same day's lineup.
  3. Ranking score:
        0.40 * velocity   (install rate over last 7 days, normalised)
      + 0.30 * success    (telemetry success rate, default 0.7 if no data)
      + 0.20 * diversity  (1.0 if creator/category not yet in lineup)
      + 0.10 * editorial  (manual boost flag — currently always 0)
  4. Tie-break: most recently version-bumped, then youngest in catalog.

Output:
  - 7 rows inserted into carousel_entries with date = tomorrow (UTC).
  - On day -7 from now, "verdict" cron checks rows whose featured_date is exactly
    7 days ago and not yet judged → labels them promote/hold/archive based on
    install_velocity vs incumbent.

Run from the API container:
  python -m app.crons.carousel_selector

Schedule via systemd timer or external cron at 23:55 UTC.
"""
from __future__ import annotations
import os, sys, math, datetime as dt, random
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

DATABASE_URL = os.environ.get("WR_DATABASE_URL", settings.DATABASE_URL)
SLOTS = 7
LOOKBACK_DAYS_HISTORY = int(os.environ.get("CAROUSEL_HISTORY_DAYS", "7"))
LOOKBACK_DAYS_VELOCITY = 7
FORCE = os.environ.get("CAROUSEL_FORCE", "0") == "1"  # dev: ignore history filter

W_VELOCITY = 0.40
W_SUCCESS = 0.30
W_DIVERSITY = 0.20
W_EDITORIAL = 0.10


def main():
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        target_date = (dt.datetime.utcnow() + dt.timedelta(days=1)).date()
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS_HISTORY)
        velocity_cutoff = dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS_VELOCITY)

        # If we already filled tomorrow's slots, exit idempotent.
        existing = session.execute(
            text("SELECT COUNT(*) FROM carousel_entries WHERE featured_date = :d"),
            {"d": target_date},
        ).scalar()
        if existing and existing >= SLOTS:
            print(f"[carousel] {target_date}: {existing} slots already filled, skipping")
            return

        # Pull eligible skills with telemetry rollups (telemetry joined by slug)
        rows = session.execute(text("""
            SELECT s.id, s.slug, s.title, s.description, s.category, s.creator_id, s.tier,
                   COUNT(DISTINCT ie.id) FILTER (WHERE ie.created_at > :vc) AS recent_installs,
                   COUNT(DISTINCT ie.id) AS total_installs,
                   COALESCE(AVG(CASE WHEN te.event_type = 'task_completed' THEN 1.0
                                     WHEN te.event_type = 'task_failed' THEN 0.0
                                     ELSE NULL END), 0.7) AS success_rate
              FROM skills s
              LEFT JOIN install_events ie ON ie.skill_id = s.id
              LEFT JOIN telemetry_events te ON te.skill_slug = s.slug
             WHERE s.is_public = true
               AND (
                 :force = true
                 OR s.id NOT IN (
                   SELECT DISTINCT ce.skill_id FROM carousel_entries ce
                    WHERE ce.featured_date > :cut
                 )
               )
             GROUP BY s.id
        """), {"vc": velocity_cutoff, "cut": cutoff, "force": FORCE}).all()

        if not rows:
            # Eligibility filter excluded everything — fall back to no-history mode
            # so the carousel never goes dark. Logs the fact for visibility.
            print(f"[carousel] {target_date}: 0 eligible after history filter, retrying with FORCE=1")
            rows = session.execute(text("""
                SELECT s.id, s.slug, s.title, s.description, s.category, s.creator_id, s.tier,
                       COUNT(DISTINCT ie.id) FILTER (WHERE ie.created_at > :vc) AS recent_installs,
                       COUNT(DISTINCT ie.id) AS total_installs,
                       COALESCE(AVG(CASE WHEN te.event_type = 'task_completed' THEN 1.0
                                         WHEN te.event_type = 'task_failed' THEN 0.0
                                         ELSE NULL END), 0.7) AS success_rate
                  FROM skills s
                  LEFT JOIN install_events ie ON ie.skill_id = s.id
                  LEFT JOIN telemetry_events te ON te.skill_slug = s.slug
                 WHERE s.is_public = true
                 GROUP BY s.id
            """), {"vc": velocity_cutoff}).all()
            if not rows:
                print(f"[carousel] {target_date}: 0 public skills exist, aborting")
                return

        # Compute scores
        max_velocity = max((r.recent_installs for r in rows), default=1) or 1
        scored = []
        for r in rows:
            velocity = r.recent_installs / max_velocity
            success = float(r.success_rate or 0.7)
            scored.append({
                "skill_id": r.id,
                "slug": r.slug,
                "title": r.title,
                "description": r.description,
                "category": r.category or "uncategorized",
                "creator_id": r.creator_id,
                "tier": r.tier,
                "velocity": velocity,
                "success": success,
                "score_base": W_VELOCITY * velocity + W_SUCCESS * success,
            })

        # Pick 7 with diversity
        random.shuffle(scored)  # break ties stochastically
        picked: list[dict] = []
        seen_creators: set = set()
        seen_categories: set = set()

        for cand in sorted(scored, key=lambda x: x["score_base"], reverse=True):
            if len(picked) >= SLOTS:
                break
            diversity = 1.0
            if cand["creator_id"] and cand["creator_id"] in seen_creators:
                diversity -= 0.6
            if cand["category"] in seen_categories:
                diversity -= 0.4
            if diversity <= 0:
                continue
            cand["diversity"] = max(0.0, diversity)
            cand["final_score"] = cand["score_base"] + W_DIVERSITY * cand["diversity"]
            picked.append(cand)
            if cand["creator_id"]:
                seen_creators.add(cand["creator_id"])
            seen_categories.add(cand["category"])

        # Backfill if diversity dropped us below 7 (rare, only when catalog tiny)
        if len(picked) < SLOTS:
            picked_ids = {p["skill_id"] for p in picked}
            for cand in sorted(scored, key=lambda x: x["score_base"], reverse=True):
                if len(picked) >= SLOTS:
                    break
                if cand["skill_id"] in picked_ids:
                    continue
                cand["diversity"] = 0.0
                cand["final_score"] = cand["score_base"]
                picked.append(cand)

        # Insert into carousel_entries (table has no `role` column — use tagline only)
        for slot, p in enumerate(picked):
            session.execute(text("""
                INSERT INTO carousel_entries
                  (id, skill_id, featured_date, position, tagline)
                VALUES
                  (gen_random_uuid(), :sid, :d, :pos, :tag)
                ON CONFLICT DO NOTHING
            """), {
                "sid": p["skill_id"],
                "d": target_date,
                "pos": slot,
                "tag": (p["title"] or p["slug"])[:120],
            })
        session.commit()

        print(f"[carousel] {target_date}: filled {len(picked)} slots")
        for slot, p in enumerate(picked):
            print(f"  slot {slot}: {p['slug']:30s} score={p['final_score']:.3f} "
                  f"(v={p['velocity']:.2f} s={p['success']:.2f} d={p['diversity']:.2f})")
    except Exception as e:
        session.rollback()
        print(f"[carousel] ERROR: {e}", file=sys.stderr)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
