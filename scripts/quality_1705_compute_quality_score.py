"""scripts/quality_1705_compute_quality_score.py — Phase C scoring v2.

Computes a 0-10 quality_score per skill via weighted average of catalog
hygiene signals. Runs nightly via the existing recipes-publish-watchdog
cron and on every publish event.

v2 changes (2026-05-17, post-launch calibration):
- install_score: 0 installs now scores 5.0 (NEUTRAL) instead of 0.0. Rationale:
  with a freshly-launched catalog, 80%+ of skills have 0 installs by definition.
  Penalising 0 installs at 2.0 points (weight 0.20) was punishing every new skill
  by default. The new formula maps install_count to a 5..10 BONUS axis only —
  no penalty for being new, real lift for proven adoption.
- description_score: now handles descriptions that lead with articles
  ("A meta-skill that...", "The plan-doc that...") by looking at words 2-5
  for the outcome verb. Previously skills like `larry` and `plan-for-goal`
  got 5.0 instead of 10.0 just for grammar.
- unhappy_paths ladder: gives ≥3 entries with ≥80-char avg a 9.0 (was no such
  bucket — jumped from 7 to 10 only at 5+ entries). This rewards skills that
  hit "real coverage" without requiring exhaustive enumeration.
- Weights rebalanced: install 0.10 (was 0.20), desc 0.30 (was 0.25),
  unhappy 0.30 (was 0.20), fresh 0.20 (was 0.25), smoke 0.10 (unchanged).
  Net: install penalty 50% smaller; content axes (desc + unhappy) carry 60%
  of the score (was 45%).

Anchor calibration (2026-05-17 dry-run): the following Adam-named reference
skills all score ≥8.0 under v2 (target: a known-good skill should never
score below 8.0):
  larry 8.10, brainstorming 8.90, chef 9.27, client-reporter 9.40,
  graphify 9.33, plan-for-goal 9.23, ruthless-mentor 9.23,
  super-memory 8.47, skill-creator 9.23

Catalog mean: 6.69 → 8.12. Median 8.03. Skills below 6 dropped from 26 → 0.
Skills remaining at 6.80 (data-pipeline, cold-outreach, seo-audit-engine,
code-review) need DESCRIPTION REWRITES — formula is now correctly surfacing
content debt rather than punishing them for being new.

Formula:
  install_score (5..10):    0 installs → 5.0 (neutral floor). Some installs →
                            mapped 6..10 by percentile within installs > 0.
  freshness_score (0..10):  10 if last_verified within 30d; linear decay to
                            0 at 365d. Missing → neutral 5.0 (was 0.0).
  description_score (0..10): 10 if ≥100 chars AND first non-article word is
                              an outcome verb; 7 if outcome verb but <100c;
                              6 if ≥100c but no verb after article;
                              5 if ≥100c no verb at all; 3 if 60-99c;
                              0 if <60c.
  unhappy_paths_score (0..10): 10 if ≥5 paths avg≥80c; 9 if ≥3 paths avg≥80c;
                                8 if ≥3 paths avg≥50c; 4 if ≥1 path; 0 none.
  smoke_score (0..10):      placeholder 7.0 (smoke test infra deferred).
  age cap:                  computed score capped at 8.5 if skill <14 days old.

  weights:                  install=0.10, fresh=0.20, desc=0.30,
                            unhappy=0.30, smoke=0.10

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

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parent.parent

# Outcome-verb dictionary — descriptions leading with one of these (or having
# one as the first non-article word) score full points on the description axis.
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
    # v2 additions — verbs that show up in real catalog descriptions
    "use", "author", "authors", "plan", "plans",
    "decompose", "decomposes", "stress", "write", "writes",
    "spawn", "spawns", "recover", "recovers", "route", "routes",
    "evolve", "evolves", "harvest", "harvests", "install", "installs",
    "synthesize", "synthesizes", "stress-test", "rank", "ranks",
    "diagnose", "discover", "discovers",
}

# Articles / pronouns that often start descriptions; if present, look past
# them for the real verb.
DESC_LEADING_ARTICLES = {
    "a", "an", "the", "this", "that", "these", "those",
    "it", "one", "your", "my", "our",
}


def get_db_url() -> str:
    url = os.environ.get("WR_DATABASE_URL")
    if url:
        return url
    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "alembic.ini")
    return cfg["alembic"]["sqlalchemy.url"]


def _install_score(install_count: int, all_counts: list[int]) -> float:
    """v2: bonus axis only. 0 installs = neutral 5.0; some installs = 6..10.

    Rationale: at catalog launch, 80%+ of skills have 0 installs. Treating
    0 as a 0.0 score punishes every skill that hasn't had time to be
    discovered yet. The formula now ONLY rewards skills with proven adoption,
    never penalises skills for being new.
    """
    if not all_counts:
        return 5.0
    nonzero = sorted(c for c in all_counts if c > 0)
    if not nonzero:
        return 5.0  # whole catalog is new — everyone gets neutral
    if install_count == 0:
        return 5.0  # NEUTRAL FLOOR — was 0.0 in v1
    # Map percentile within installed skills to 6..10
    rank = sum(1 for c in nonzero if c <= install_count)
    return min(10.0, 6.0 + 4.0 * (rank / len(nonzero)))


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
    """v2: missing last_verified now scores 5.0 (neutral), was 0.0."""
    last_verified = _to_dt(last_verified)
    if not last_verified:
        return 5.0  # NEUTRAL FLOOR — was 0.0 in v1
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
    """v2: skips leading articles before checking for outcome verb.

    A description starting with 'A meta-skill that...' or 'The plan-doc that...'
    is given a fair shot at the outcome-verb bonus by looking at words 2-5.
    """
    if not description:
        return 0.0
    desc = description.strip()
    if len(desc) < 60:
        return 0.0
    words = re.split(r"\W+", desc)
    if not words:
        return 0.0
    first = words[0].lower()
    # v2: look past leading articles for the real verb
    if first in DESC_LEADING_ARTICLES and len(words) > 1:
        for w in words[1:5]:
            if w.lower() in OUTCOME_VERBS:
                return 10.0 if len(desc) >= 100 else 7.0
        # Article + no verb in next 4 words → still useful description, just
        # not outcome-led — score above generic but below ideal
        return 6.0 if len(desc) >= 100 else 4.0
    if first in OUTCOME_VERBS:
        return 10.0 if len(desc) >= 100 else 7.0
    return 5.0 if len(desc) >= 100 else 3.0


def _parse_unhappy_paths(readme: str | None) -> list[dict]:
    """Extract unhappy_paths from readme YAML frontmatter. Returns [] on any parse issue."""
    if not readme or not readme.startswith("---") or yaml is None:
        return []
    try:
        end = readme.index("\n---", 3)
    except ValueError:
        return []
    fm_text = readme[3:end].strip("\n")
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return []
    paths = data.get("unhappy_paths") if isinstance(data, dict) else None
    if not isinstance(paths, list):
        return []
    cleaned: list[dict] = []
    for p in paths:
        if not isinstance(p, dict):
            continue
        c = p.get("condition")
        r = p.get("recovery")
        if isinstance(c, str) and isinstance(r, str) and c.strip() and r.strip():
            cleaned.append({"condition": c.strip(), "recovery": r.strip()})
    return cleaned


def _unhappy_paths_score(readme: str | None) -> float:
    """v2: new bucket at n≥3, avg≥80c → 9.0 (was no such bucket).

    Old ladder jumped from 7 (3 paths) to 10 (5+ paths) with nothing
    in between. New ladder rewards skills that hit real depth (≥80-char
    avg per entry) at n≥3 without forcing them to enumerate 5+ paths.
    """
    paths = _parse_unhappy_paths(readme)
    n = len(paths)
    if n == 0:
        return 0.0
    avg_text_len = sum(len(p["condition"]) + len(p["recovery"]) for p in paths) / n
    if n >= 5 and avg_text_len >= 80:
        return 10.0
    if n >= 3 and avg_text_len >= 80:
        return 9.0  # NEW BUCKET in v2
    if n >= 3 and avg_text_len >= 50:
        return 8.0  # bumped from 7.0
    if n >= 1:
        return 4.0  # bumped from 3.0 (a token attempt is worth something)
    return 0.0


def compute_score(
    install_count: int,
    last_verified,
    description: str | None,
    created_at,
    all_install_counts: list[int],
    now: datetime,
    readme: str | None = None,
) -> float:
    install_s = _install_score(install_count, all_install_counts)
    fresh_s = _freshness_score(last_verified, now)
    desc_s = _description_score(description)
    unhappy_s = _unhappy_paths_score(readme)
    # Smoke test infra deferred — neutral placeholder
    smoke_s = 7.0

    # v2 weights (was: install=0.20, fresh=0.25, desc=0.25, unhappy=0.20, smoke=0.10)
    raw = (
        install_s * 0.10
        + fresh_s * 0.20
        + desc_s * 0.30
        + unhappy_s * 0.30
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
            "created_at, quality_score, readme FROM skills "
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
                    readme=r.readme,
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
                    r.created_at, all_install_counts, now, r.readme,
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
