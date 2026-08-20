"""backfill_skill_titles.py — fix Skill.title where it equals slug.

Symptom (validated 2026-05-19 on prod):
  ~16% of public skills have `title == slug` in the skills table. The carousel
  tagline backfill (sister script) fixed taglines, but the rendered card STILL
  shows "gh-fix-ci" as the headline because the card uses Skill.title.

Derivation logic lives in app/skill_title.py (issue #155) — shared with the
publish-time guard in app/publisher_routes.py so new skills cannot land
title-less going forward. This script is now a thin one-time-sweep runner
over that shared logic for historical rows.

Skips (idempotent):
  - Skills whose title already differs from slug
  - Skills where derived title equals existing title
  - Skills where no derivation improved on the slug

Usage:
    .venv/bin/python -m app.scripts.backfill_skill_titles            # apply
    .venv/bin/python -m app.scripts.backfill_skill_titles --dry-run  # preview

Exits 0 on success, prints summary plus per-row diff.
"""

from __future__ import annotations

import argparse
import sys

from app.database import SessionLocal
from app.models import Skill
from app.skill_title import derive_title, parse_frontmatter_field


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill skill titles where title == slug.")
    parser.add_argument("--dry-run", action="store_true", help="Print proposed changes without committing.")
    parser.add_argument("--limit", type=int, default=None, help="Cap rows processed (debug).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = db.query(Skill).filter(Skill.title == Skill.slug)
        if args.limit:
            q = q.limit(args.limit)
        candidates = q.all()

        if not candidates:
            print("No skills with title == slug — nothing to backfill.")
            return 0

        print(f"Found {len(candidates)} skills with title == slug. Processing…")
        changed = 0
        skipped = 0
        for s in candidates:
            readme = getattr(s, "readme", None) or ""
            new_title = derive_title(s.slug, readme)
            if not new_title or new_title == s.title:
                skipped += 1
                continue
            # Determine source for log
            if parse_frontmatter_field(readme, "title"):
                source = "frontmatter:title"
            elif (
                parse_frontmatter_field(readme, "name") and parse_frontmatter_field(readme, "name") != s.slug
            ):
                source = "frontmatter:name"
            else:
                source = "slug→title-case"
            print(f"  {s.slug}:")
            print(f"    OLD: {s.title!r}")
            print(f"    NEW: {new_title!r}  (source: {source})")
            if not args.dry_run:
                s.title = new_title
            changed += 1

        if args.dry_run:
            print(f"\n[DRY-RUN] would update {changed} titles; {skipped} unchanged.")
        else:
            db.commit()
            print(f"\nUpdated {changed} skill titles; {skipped} unchanged.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
