#!/usr/bin/env python3
"""scripts/backfill_skill_description_from_tarball.py

Repair Skill rows whose `description` column is empty, a YAML literal
indicator (`|`, `>`), or shorter than 20 chars by reading the canonical
SKILL.md frontmatter out of the latest published tarball.

This is a one-shot for the bug surfaced by RCP-PUB-2026-05-18 (minto@1.0.1
landed with description='|' because the row had been seeded by a prior code
path that didn't yaml-parse the frontmatter, and the publish endpoint did
not overwrite the description on an existing row). The corresponding fix
in app/publisher_routes.py prevents new occurrences; this script cleans up
the historical state.

Usage:
    set -a && source .env && set +a
    .venv/bin/python scripts/backfill_skill_description_from_tarball.py [--dry-run] [--slug <slug>]

Exit codes:
    0 — clean run (zero rows or all repaired)
    1 — partial failure
    2 — usage error
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tarfile
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text

SKILLS_DIR = Path("/var/lib/recipes-skills")
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def extract_description(tarball_path: Path) -> str | None:
    """Pull `description` out of SKILL.md frontmatter inside a tarball."""
    try:
        with tarfile.open(tarball_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/SKILL.md") or member.name == "SKILL.md":
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    text_content = f.read().decode("utf-8", errors="replace")
                    m = FM_RE.match(text_content)
                    if not m:
                        return None
                    try:
                        data = yaml.safe_load(m.group(1)) or {}
                    except yaml.YAMLError:
                        return None
                    desc = data.get("description")
                    if not isinstance(desc, str):
                        return None
                    return " ".join(desc.split()).strip()
    except (tarfile.TarError, OSError):
        return None
    return None


def find_latest_tarball(slug: str) -> Path | None:
    slug_dir = SKILLS_DIR / slug
    if not slug_dir.exists():
        return None
    tarballs = sorted(slug_dir.glob("*.tar.gz"))
    return tarballs[-1] if tarballs else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
    ap.add_argument("--slug", help="Only repair this slug (default: scan all dirty rows).")
    args = ap.parse_args()

    url = os.environ.get("WR_DATABASE_URL")
    if not url:
        print("ERROR: WR_DATABASE_URL not set", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.connect() as conn:
        if args.slug:
            rows = conn.execute(
                text("SELECT slug, description FROM skills WHERE slug = :s"),
                {"s": args.slug},
            ).fetchall()
        else:
            rows = conn.execute(text("""
                SELECT slug, description
                FROM skills
                WHERE is_public = true
                  AND (
                    length(coalesce(description, '')) < 20
                    OR description IN ('|', '>', '|-', '>-')
                  )
                ORDER BY slug
            """)).fetchall()

    if not rows:
        print("No dirty descriptions found. Nothing to do.")
        return 0

    print(f"Found {len(rows)} skill(s) to inspect:")
    fixed = 0
    failed = 0
    skipped = 0
    for row in rows:
        slug = row[0]
        old = row[1] or ""
        tarball = find_latest_tarball(slug)
        if tarball is None:
            print(f"  [SKIP] {slug}: no tarball at {SKILLS_DIR / slug}/")
            skipped += 1
            continue
        new_desc = extract_description(tarball)
        if not new_desc:
            print(f"  [FAIL] {slug}: could not extract description from {tarball.name}")
            failed += 1
            continue
        if new_desc == old.strip():
            print(f"  [OK]   {slug}: already matches tarball")
            continue
        preview = new_desc[:80] + ("…" if len(new_desc) > 80 else "")
        print(f"  [FIX]  {slug}: '{old[:30]}' → '{preview}' ({len(new_desc)} chars)")
        if not args.dry_run:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE skills SET description = :d WHERE slug = :s"),
                    {"d": new_desc, "s": slug},
                )
        fixed += 1

    print(f"\nSummary: fixed={fixed}  failed={failed}  skipped={skipped}  dry_run={args.dry_run}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
