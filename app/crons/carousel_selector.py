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

import datetime as dt
import os
import random
import sys

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


# ── Hoisted helpers (testable) ─────────────────────────────────────────────
# 2026-05-19 (atomic-habits CRIT 7): these were inline inside main(). Hoisted
# so tests/test_carousel_selector.py can exercise them without spinning up a
# DB. They are pure functions of a candidate dict + a tagline string. The
# slot1_quality_check mirrors the contract in skill `carousel-content-quality-gate`.

import re as _re


def _word_boundary_trim(text: str, max_len: int = 120) -> str:
    """Trim text to max_len at a word boundary and append ellipsis if truncated."""
    text = text.rstrip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rstrip()
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space].rstrip()
    return truncated + "\u2026"


def derive_tagline(p: dict) -> str:
    """Return the tagline a candidate would publish. description-first.

    `p` is the scored-candidate dict built earlier in main() — has keys
    description, title, slug. We cap at 120 chars to match the DB schema
    (carousel_entries.tagline String(512), but the on-card render budget is
    ~80-120 chars and we want headroom for ellipses on the FE side).
    """
    desc = (p.get("description") or "").strip()
    if desc:
        return _word_boundary_trim(desc, 120)
    return _word_boundary_trim((p.get("title") or p.get("slug") or ""), 120)


def slot1_quality_check(p: dict, tagline: str) -> tuple[bool, str]:
    """Slot-1 lint per skill `carousel-content-quality-gate`.

    Returns (passed, drop_reason). Drop reasons are stable identifiers used by
    downstream tooling (weekly retro, rejects log) — DO NOT rename without
    updating the skill and the watchdog canary.
    """
    title = (p.get("title") or "").strip()
    slug = (p.get("slug") or "").strip()
    t = (tagline or "").strip()
    if t.lower() == title.lower():
        return False, "tagline_equals_title"
    if len(t) < 20:
        return False, f"tagline_too_short:{len(t)}"
    if _re.fullmatch(r"[A-Z][a-z]+", t):
        return False, "tagline_single_word"
    stripped_slug = slug.replace("-", "").replace("_", "")
    if len(stripped_slug) < 4 or (len(slug) < 6 and "-" not in slug):
        return False, "slug_too_thin"
    return True, "ok"


def assign_role(slot_1idx: int, p: dict | None = None) -> str:
    """Minimal role assignment. slots 1-5 → new-capability, 6-7 → experimental.

    The richer `_assign_role` in app/carousel/selector.py inspects same-category
    older skills to pick `replaces` — this lightweight version is what the
    systemd-timer cron uses to keep the write path fast (no per-skill JOIN).
    """
    if slot_1idx >= 6:
        return "experimental"
    return "new-capability"


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
        rows = session.execute(
            text("""
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
               AND s.description IS NOT NULL
               AND char_length(trim(s.description)) >= 20
               AND lower(trim(s.description)) <> lower(trim(s.title))
               AND lower(trim(s.description)) <> lower(trim(s.slug))
               AND (
                 :force = true
                 OR s.id NOT IN (
                   SELECT DISTINCT ce.skill_id FROM carousel_entries ce
                    WHERE ce.featured_date > :cut
                 )
               )
             GROUP BY s.id
        """),
            {"vc": velocity_cutoff, "cut": cutoff, "force": FORCE},
        ).all()

        if not rows:
            # Eligibility filter excluded everything — fall back to no-history mode
            # so the carousel never goes dark. Logs the fact for visibility.
            print(f"[carousel] {target_date}: 0 eligible after history filter, retrying with FORCE=1")
            rows = session.execute(
                text("""
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
                   AND s.description IS NOT NULL
                   AND char_length(trim(s.description)) >= 20
                   AND lower(trim(s.description)) <> lower(trim(s.title))
                   AND lower(trim(s.description)) <> lower(trim(s.slug))
                 GROUP BY s.id
            """),
                {"vc": velocity_cutoff},
            ).all()
            if not rows:
                print(f"[carousel] {target_date}: 0 public skills exist, aborting")
                return

        # Compute scores
        max_velocity = max((r.recent_installs for r in rows), default=1) or 1
        scored = []
        for r in rows:
            velocity = r.recent_installs / max_velocity
            success = float(r.success_rate or 0.7)
            scored.append(
                {
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
                }
            )

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

        # Slot-1 pre-promotion gate — re-pick if it fails (helpers hoisted to module level for testability — see derive_tagline / slot1_quality_check / assign_role below)
        if picked:
            rejected_skill_ids: set = set()
            attempts = 0
            while attempts < 5:
                tag = derive_tagline(picked[0])
                ok, reason = slot1_quality_check(picked[0], tag)
                if ok:
                    break
                # Drop slot-1 candidate, log reject, promote next
                rejects_dir = os.path.expanduser("~/.hermes/state/carousel-rejects")
                try:
                    os.makedirs(rejects_dir, exist_ok=True)
                    log_path = os.path.join(rejects_dir, f"{target_date}.log")
                    with open(log_path, "a") as fh:
                        fh.write(
                            f'{{"date": "{target_date}", "slug": "{picked[0].get("slug", "")}", "drop_reason": "{reason}"}}\n'
                        )
                # Rationale: reject-log is best-effort; filesystem errors must not crash the cron
                except Exception as _logerr:  # noqa: BLE001
                    print(f"[carousel] WARN: could not log slot-1 reject: {_logerr}", file=sys.stderr)
                print(
                    f"[carousel] slot-1 rejected: {picked[0].get('slug', '?')} reason={reason}",
                    file=sys.stderr,
                )
                # Promote the next candidate by removing index 0
                rejected_skill_ids.add(picked[0].get("skill_id"))
                if len(picked) > 1:
                    picked = picked[1:]
                else:
                    break
                attempts += 1
            if attempts >= 5:
                print(
                    f"[carousel] WARN: slot-1 quality gate exhausted retries on {target_date}",
                    file=sys.stderr,
                )

            # Backfill the tail to SLOTS — the slot-1 gate drop (picked[1:])
            # shrinks the lineup; refill from scored so count stays at 7.
            # (carousel-content-quality-gate drop-shrink bug, fixed 2026-06-18.)
            if len(picked) < SLOTS:
                picked_ids = {p["skill_id"] for p in picked}
                for cand in sorted(scored, key=lambda x: x["score_base"], reverse=True):
                    if len(picked) >= SLOTS:
                        break
                    if cand["skill_id"] in picked_ids or cand["skill_id"] in rejected_skill_ids:
                        continue
                    cand["diversity"] = 0.0
                    cand["final_score"] = cand["score_base"]
                    picked.append(cand)
                    picked_ids.add(cand["skill_id"])

        # Insert with slot (1-indexed), role, score, and description-derived tagline
        for idx, p in enumerate(picked):
            slot_1idx = idx + 1
            tagline = derive_tagline(p)
            role = assign_role(slot_1idx, p)
            session.execute(
                text("""
                INSERT INTO carousel_entries
                  (id, skill_id, featured_date, position, slot, role, score, tagline)
                VALUES
                  (gen_random_uuid(), :sid, :d, :pos, :slot, :role, :score, :tag)
                ON CONFLICT DO NOTHING
            """),
                {
                    "sid": p["skill_id"],
                    "d": target_date,
                    "pos": idx,  # backward-compat 0-indexed
                    "slot": slot_1idx,  # 1-indexed
                    "role": role,
                    "score": float(p.get("final_score", 0.0)),
                    "tag": tagline,
                },
            )
        session.commit()

        print(f"[carousel] {target_date}: filled {len(picked)} slots")
        for slot, p in enumerate(picked):
            print(
                f"  slot {slot}: {p['slug']:30s} score={p['final_score']:.3f} "
                f"(v={p['velocity']:.2f} s={p['success']:.2f} d={p['diversity']:.2f})"
            )
    # Rationale: outer try/except for cron top-level; DB commit failure must log+reraise
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[carousel] ERROR: {e}", file=sys.stderr)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
