"""scripts/import_skill_metadata.py — Backfill skills.related_skills from SKILL.md frontmatter.

Stage 1 (G15) of the Skill-Graph plan. Idempotent: re-running overwrites with
the latest frontmatter declarations. Dry-run by default; pass --commit to write.

Sources scanned (in order, first match wins):
  1. ~/.hermes/skills/<category>/<slug>/SKILL.md  (main authoring location)
  2. ~/.hermes/skills/<slug>/SKILL.md              (legacy flat layout)

For each public skill in the DB:
  - locate its SKILL.md by slug
  - parse YAML frontmatter
  - read `related_skills:` (supports both inline list and YAML block list)
  - normalise slugs to lowercase
  - drop self-references and slugs not present in DB
  - upsert into skills.related_skills

Output: per-skill diff log + summary counts. Exit 0 on success, 1 on errors.

Usage:
    # local dev (SQLite test DB, dry-run)
    python scripts/import_skill_metadata.py --dry-run

    # against prod (Postgres at $WR_DATABASE_URL)
    python scripts/import_skill_metadata.py --commit

    # custom skills root
    python scripts/import_skill_metadata.py --skills-root /var/lib/recipes-skills --commit
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Ensure repo root on path so `app.*` imports work when run as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Skill  # noqa: E402


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def parse_frontmatter_from_text(text: str) -> Optional[dict]:
    """Parse YAML frontmatter from a SKILL.md text blob (in-memory)."""
    if not text:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def parse_frontmatter(path: Path) -> Optional[dict]:
    """Read SKILL.md and return parsed YAML frontmatter dict (or None)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return parse_frontmatter_from_text(text)


def coerce_related(raw) -> List[str]:
    """Accept any of:
        related_skills: [a, b, c]
        related_skills:
          - a
          - b
        related_skills: a, b, c        (single string, comma-separated — tolerate)
    Return a normalised list of lowercase slugs.
    """
    if raw is None:
        return []
    items: List[str] = []
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw if x is not None]
    elif isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        return []

    out: List[str] = []
    seen: set = set()
    for s in items:
        norm = s.lower().strip()
        if not norm or norm in seen:
            continue
        if not SLUG_RE.match(norm):
            # Skip malformed slugs (keeps DB clean)
            continue
        seen.add(norm)
        out.append(norm)
    return out


def find_skill_md(skills_root: Path, slug: str) -> Optional[Path]:
    """Locate SKILL.md for a given slug. Returns None if not found.

    Search order:
      1. <root>/<category>/<slug>/SKILL.md   (main convention, any category subdir)
      2. <root>/<slug>/SKILL.md               (flat fallback)
    """
    # Flat layout first (cheap exact path)
    flat = skills_root / slug / "SKILL.md"
    if flat.exists():
        return flat
    # Recursive search (one level deep — categories/<slug>/SKILL.md)
    for category_dir in skills_root.iterdir():
        if not category_dir.is_dir():
            continue
        candidate = category_dir / slug / "SKILL.md"
        if candidate.exists():
            return candidate
    return None


def extract_frontmatter_from_tarball(tarball_dir: Path, slug: str) -> Optional[dict]:
    """Read SKILL.md frontmatter directly from the canonical tarball.

    Looks for /var/lib/recipes-skills/<slug>/<semver>.tar.gz, picks the
    highest-versioned tarball, extracts <slug>/SKILL.md, and parses YAML.
    Returns None if nothing found.

    This is the canonical source: it's what users actually download. Any
    drift between source-tree SKILL.md and tarball SKILL.md is a bug; the
    tarball is the truth.
    """
    import tarfile
    import re as _re

    skill_dir = tarball_dir / slug
    if not skill_dir.exists():
        return None

    tarballs = sorted(skill_dir.glob("*.tar.gz"))
    if not tarballs:
        return None

    # Pick the highest semver lexicographically (good enough for x.y.z names)
    chosen = tarballs[-1]
    try:
        with tarfile.open(chosen, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/SKILL.md") or member.name == "SKILL.md":
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    text = f.read().decode("utf-8", errors="replace")
                    m = _re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, _re.DOTALL)
                    if not m:
                        return None
                    try:
                        data = yaml.safe_load(m.group(1))
                    except yaml.YAMLError:
                        return None
                    return data if isinstance(data, dict) else None
    except (tarfile.TarError, OSError):
        return None
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--skills-root",
        default=os.path.expanduser("~/.hermes/skills"),
        help="Source-tree root to search for SKILL.md files (default: ~/.hermes/skills)",
    )
    p.add_argument(
        "--tarball-root",
        default="/var/lib/recipes-skills",
        help="Canonical tarball storage. When present, frontmatter is read directly "
             "from <tarball-root>/<slug>/<semver>.tar.gz (preferred over source tree).",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Persist changes (default: dry-run, log only)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show per-skill diff even when no change",
    )
    args = p.parse_args()

    skills_root = Path(args.skills_root)
    tarball_root = Path(args.tarball_root)
    if not skills_root.exists() and not tarball_root.exists():
        print(f"ERROR: neither skills-root nor tarball-root exists: {skills_root} / {tarball_root}", file=sys.stderr)
        return 1

    print(f"📂 source tree: {skills_root} ({'exists' if skills_root.exists() else 'missing'})")
    print(f"📦 tarball root: {tarball_root} ({'exists' if tarball_root.exists() else 'missing'})")
    print(f"📦 mode: {'COMMIT' if args.commit else 'dry-run'}")

    db = SessionLocal()
    try:
        skills = db.query(Skill).filter(Skill.is_public.is_(True)).order_by(Skill.slug).all()
        print(f"🔍 scanning {len(skills)} public skills…\n")

        all_db_slugs = {s.slug for s in skills}
        stats = {
            "scanned": 0,
            "no_skill_md": 0,
            "no_frontmatter": 0,
            "no_related_field": 0,
            "would_change": 0,
            "unchanged": 0,
            "committed": 0,
            "self_refs_dropped": 0,
            "dangling_dropped": 0,
        }

        for skill in skills:
            stats["scanned"] += 1

            # Source priority:
            #   1. Tarball (canonical — what users download)
            #   2. Source-tree (~/.hermes/skills) for skills not yet tarballed
            #   3. skills.readme column (productized skills that ship with seed-data
            #      readme but no tarball yet — e.g. client-reporter, cold-outreach)
            fm: Optional[dict] = None
            source_label = ""
            if tarball_root.exists():
                fm = extract_frontmatter_from_tarball(tarball_root, skill.slug)
                if fm is not None:
                    source_label = "tarball"
            if fm is None and skills_root.exists():
                md_path = find_skill_md(skills_root, skill.slug)
                if md_path is not None:
                    fm = parse_frontmatter(md_path)
                    if fm is not None:
                        source_label = "src-tree"
            if fm is None and skill.readme:
                fm = parse_frontmatter_from_text(skill.readme)
                if fm is not None:
                    source_label = "db-readme"

            if fm is None:
                stats["no_skill_md"] += 1
                if args.verbose:
                    print(f"  · {skill.slug}: no frontmatter found in tarball, source-tree, or DB readme")
                continue

            raw = fm.get("related_skills")
            if raw is None:
                stats["no_related_field"] += 1
                if args.verbose:
                    print(f"  · {skill.slug}: no related_skills declared")
                continue

            declared = coerce_related(raw)

            # Drop self-references
            cleaned: List[str] = []
            for s in declared:
                if s == skill.slug:
                    stats["self_refs_dropped"] += 1
                    continue
                if s not in all_db_slugs:
                    stats["dangling_dropped"] += 1
                    continue
                cleaned.append(s)

            # Compute diff
            current = skill.related_skills or []
            if list(current) == cleaned:
                stats["unchanged"] += 1
                if args.verbose:
                    print(f"  · {skill.slug}: unchanged ({len(cleaned)} related)")
                continue

            stats["would_change"] += 1
            print(f"  ✎ {skill.slug} [{source_label}]: {current} → {cleaned}")

            if args.commit:
                skill.related_skills = cleaned
                stats["committed"] += 1

        if args.commit:
            db.commit()
            print("\n✅ committed")
        else:
            print("\n🔍 dry-run — no changes written")

        print("\n──────── summary ────────")
        for k, v in stats.items():
            print(f"  {k:<22} {v}")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
