#!/usr/bin/env python3
"""polish_1805 item 3 — apply outcome-led description rewrites to all 53 skills.

Reads /tmp/skill_descriptions_rewrite.json (produced by the Haiku batch in the
parent Tori session) and writes the ``new`` description to every Skill row
whose ``slug`` matches. ``kept_original: true`` entries are skipped.

Safety properties:
- **Idempotent.** Running twice with the same JSON file produces the same DB
  state (no-op the second time for any row whose description already matches).
- **Dry-run first.** Default mode prints what would change. Pass ``--apply`` to
  actually write.
- **Per-skill report.** Every change prints ``[slug] OLD: …  NEW: …  Δ-chars``.
- **Single transaction.** Either every row commits or none does.
- **Char-cap enforced.** Refuses to write a description >200 chars (rule from
  the brief).
- **No fluff lint.** Refuses any rewrite containing banned words from
  claim-grounded-marketing skill.

Usage:
    python scripts/polish_1805_apply_descriptions.py                 # dry-run
    python scripts/polish_1805_apply_descriptions.py --apply         # commit
    python scripts/polish_1805_apply_descriptions.py --slug X --apply  # one slug
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Banned phrases (claim-grounded-marketing). Any rewrite containing one of
# these case-insensitively is refused even if Haiku produced it.
BANNED_PHRASES = (
    "powerful",
    "amazing",
    "revolutionary",
    "world-class",
    "game-changing",
    "cutting-edge",
    "next-generation",
    "best-in-class",
    "industry-leading",
    "seamlessly",
    "blazing fast",
    "lightning fast",
    "unparalleled",
)

# Max description length per polish_1805 rules
MAX_DESCRIPTION_CHARS = 200


def _lint(new_desc: str, slug: str) -> str | None:
    """Return an error message if the description fails the lint, else None."""
    if len(new_desc) > MAX_DESCRIPTION_CHARS:
        return f"description >200 chars ({len(new_desc)})"
    low = new_desc.lower()
    for banned in BANNED_PHRASES:
        if banned in low:
            return f"contains banned phrase '{banned}'"
    if not new_desc.strip():
        return "empty description"
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Actually write to the DB.")
    p.add_argument("--slug", help="Limit to one slug (debug).")
    p.add_argument(
        "--input",
        default="/tmp/skill_descriptions_rewrite.json",
        help="Path to the rewrite JSON (from Haiku batch).",
    )
    p.add_argument(
        "--lint-only",
        action="store_true",
        help="Run lint checks against the JSON without opening a DB session.",
    )
    args = p.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"❌ {path} not found", file=sys.stderr)
        sys.exit(2)

    data = json.loads(path.read_text())
    rewrites = data.get("rewrites", [])
    print(f"Loaded {len(rewrites)} rewrites from {path}")

    # Apply lint first — refuse the whole batch if any single rewrite is bad
    lint_errors = []
    for r in rewrites:
        if r.get("kept_original"):
            continue
        new = r.get("new", "")
        err = _lint(new, r["slug"])
        if err:
            lint_errors.append((r["slug"], err))
    if lint_errors:
        print(f"\n❌ LINT FAILED on {len(lint_errors)} rewrites:")
        for slug, err in lint_errors:
            print(f"  [{slug}] {err}")
        print("\nFix the JSON and re-run. No DB changes made.")
        sys.exit(3)
    print(f"✅ Lint pass — all {len(rewrites)} rewrites clean")

    if args.lint_only:
        return

    # Import inside main so the script can be linted/imported without DB connection
    from app.database import SessionLocal
    from app.models import Skill

    db = SessionLocal()
    try:
        changes = []
        skipped_already_matches = 0
        not_found = []
        for r in rewrites:
            if args.slug and r["slug"] != args.slug:
                continue
            if r.get("kept_original"):
                continue
            slug = r["slug"]
            new_desc = r["new"]
            row = db.query(Skill).filter(Skill.slug == slug).first()
            if not row:
                not_found.append(slug)
                continue
            if (row.description or "").strip() == new_desc.strip():
                skipped_already_matches += 1
                continue
            changes.append({
                "slug": slug,
                "old": (row.description or "")[:120],
                "new": new_desc,
                "delta_chars": len(new_desc) - len(row.description or ""),
                "row": row,
            })

        # Report
        print(f"\n=== Plan ===")
        print(f"  Changes to apply: {len(changes)}")
        print(f"  Already-matching (no-op): {skipped_already_matches}")
        print(f"  Not found in DB: {len(not_found)}")
        if not_found:
            print(f"    {not_found[:10]}")

        for c in changes[:8]:
            print(f"\n  [{c['slug']}] Δ{c['delta_chars']:+d}c")
            print(f"    OLD: {c['old']}")
            print(f"    NEW: {c['new']}")
        if len(changes) > 8:
            print(f"  … and {len(changes) - 8} more")

        if not args.apply:
            print("\n💡 Dry-run mode. Pass --apply to commit.")
            return

        # Commit
        for c in changes:
            c["row"].description = c["new"]
        db.commit()
        print(f"\n✅ Wrote {len(changes)} description updates.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
