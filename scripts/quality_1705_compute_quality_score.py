"""scripts/quality_1705_compute_quality_score.py — Phase C scoring.

Computes a 0-10 quality_score per skill via weighted average of catalog
hygiene signals. Runs nightly via the existing recipes-publish-watchdog
cron and on every publish event.

Formula (per plan §3 Phase C step 6, scoped to what we can measure today):

  install_score (0..10):   percentile rank of install_count_total across
                            non-archived public skills, mapped 0..10.
                            (Skills with 0 installs cluster at low ranks.)
  freshness_score (0..10): 10 if last_verified is within 30d,
                            decays linearly to 0 at 365d.
  description_score (0..10): 10 if description >= 100 chars AND starts
                              with an outcome verb (save/generate/triage/
                              detect/build/etc.). 5 if >= 100 chars but no
                              verb. 0 if < 60 chars.
  age_cap_score (0..10):  10 unless the skill was created in the last 14d
                            — then cap any computed score at 8.5 (F8 mitigation).

  weights:                install=0.30, freshness=0.30, description=0.30, age_cap=0.10

  Deferred (will land in Phase D + later C):
    - unhappy_paths presence (requires SKILL.md backfill)
    - demo video presence (requires Phase D videos)
    - smoke test pass rate (requires Phase C container test infra)
  These three sub-scores default to 7.0 for now (neutral) so the score is
  computable; they'll be wired in once their feeders ship.

Idempotent: re-running with no signal change produces identical numbers.
Dry-run default; --commit to write.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

# Outcome-verb dictionary — descriptions leading with one of these score full
# points on the description axis. Conservative list; extend as patterns emerge.
OUTCOME_VERBS = {
    "save", "saves", "generate", "generates", "triage", "triages",
    "detect", "detects", "build", "builds", "create", "creates",
    "deploy", "deploys", "diagnose", "diagnoses", "fix", "fixes",
    "wire", "wires", "scan", "scans", "audit", "audits",
    "publish", "publishes", "run", "runs", "ship", "ships",
    "monitor", "monitors", "extract", "extracts", "convert", "converts",
    "render", "renders", "test", "tests", "validate", "validates",
    "analyze", "analyzes", "optimize", "optimizes", "manage", "manages",
    "automate", "automates", "track", "tracks", "send", "sends",
    "pull", "pulls", "post", "posts", "review", "reviews",
    "compose", "composes", "summarize", "summarizes", "search", "searches",
    "find", "finds", "watch", "watches", "rotate", "rotates",
}


def get_db_url() -> str:
    url = os.environ.get("WR_DATABASE_URL")
    if url:
        return url
    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "alembic.ini")
    return cfg["alembic"]["sqlalchemy.url"]


def _install_score(install_count: int, all_counts: list[int]) -> float:
    """Percentile rank 0..10. Skills with 0 installs score 0 unless every
    skill has 0 installs (then everyone gets a neutral 5)."""
    if not all_counts:
        return 5.0
    sorted_counts = sorted(all_counts)
    n = len(sorted_counts)
    if install_count == 0:
        return 0.0
    rank = sum(1 for c in sorted_counts if c <= install_count)
    return min(10.0, (rank / n) * 10.0)


def _to_dt(v) -> datetime | None:
    """SQLite returns DateTime as string via text(); coerce to datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _freshness_score(last_verified, now: datetime) -> float:
    last_verified = _to_dt(last_verified)
    if not last_verified:
        return 0.0
    if last_verified.tzinfo is None:
        last_verified = last_verified.replace(tzinfo=timezone.utc)
    days = (now - last_verified).days
    if days <= 30:
        return 10.0
    if days >= 365:
        return 0.0
    # Linear decay from 10 (day 30) to 0 (day 365)
    return max(0.0, 10.0 - (days - 30) / 33.5)


def _description_score(description: str | None) -> float:
    if not description:
        return 0.0
    desc = description.strip()
    if len(desc) < 60:
        return 0.0
    first_word = re.split(r"\W+", desc, maxsplit=1)[0].lower()
    if first_word in OUTCOME_VERBS:
        return 10.0 if len(desc) >= 100 else 7.0
    return 5.0 if len(desc) >= 100 else 3.0


def compute_score(
    install_count: int,
    last_verified,
    description: str | None,
    created_at,
    all_install_counts: list[int],
    now: datetime,
) -> float:
    install_s = _install_score(install_count, all_install_counts)
    fresh_s = _freshness_score(last_verified, now)
    desc_s = _description_score(description)
    # Neutral placeholders until Phase D + unhappy_paths backfill land
    unhappy_s = 7.0
    video_s = 7.0
    smoke_s = 7.0

    raw = (
        install_s * 0.20
        + fresh_s * 0.25
        + desc_s * 0.25
        + unhappy_s * 0.10
        + video_s * 0.10
        + smoke_s * 0.10
    )

    # F8 mitigation: cap at 8.5 for skills < 14 days old (no install data yet)
    created_at = _to_dt(created_at)
    if created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_days = (now - created_at).days
        if age_days < 14:
            raw = min(raw, 8.5)

    return round(raw, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit", action="store_true",
        help="Write quality_score back to DB. Default is dry-run.",
    )
    parser.add_argument(
        "--db-url", help="Override DB URL (else WR_DATABASE_URL or alembic.ini)",
    )
    args = parser.parse_args()

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_url = args.db_url or get_db_url()
    engine = create_engine(db_url, future=True)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc)

    with Session() as session:
        rows = session.execute(text(
            "SELECT id, slug, install_count, last_verified, description, "
            "created_at, quality_score FROM skills "
            "WHERE is_public = true AND is_archived = false"
        )).all()

        all_install_counts = [r.install_count or 0 for r in rows]

        diffs = []
        unchanged = 0
        try:
            for r in rows:
                new_score = compute_score(
                    install_count=r.install_count or 0,
                    last_verified=r.last_verified,
                    description=r.description,
                    created_at=r.created_at,
                    all_install_counts=all_install_counts,
                    now=now,
                )
                old_score = r.quality_score
                if old_score is None or abs((old_score or 0) - new_score) >= 0.01:
                    diffs.append({
                        "slug": r.slug,
                        "old": old_score,
                        "new": new_score,
                    })
                    if args.commit:
                        session.execute(
                            text(
                                "UPDATE skills SET quality_score = :s "
                                "WHERE id = :id"
                            ),
                            {"s": new_score, "id": r.id},
                        )
                else:
                    unchanged += 1
            if args.commit:
                session.commit()
            else:
                session.rollback()
        except Exception:
            session.rollback()
            raise

    summary = {
        "total_skills": len(rows),
        "unchanged": unchanged,
        "updated_count": len(diffs),
        "updated": diffs[:20],  # cap output
        "avg_score": round(
            sum(
                (compute_score(
                    r.install_count or 0, r.last_verified, r.description,
                    r.created_at, all_install_counts, now,
                ))
                for r in rows
            ) / max(len(rows), 1),
            2,
        ),
    }
    print(json.dumps(summary, indent=2, default=str))

    if not args.commit:
        print("\n[DRY-RUN] No changes written. Re-run with --commit to apply.")
        return 0
    print("\n[COMMITTED] quality_score updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
