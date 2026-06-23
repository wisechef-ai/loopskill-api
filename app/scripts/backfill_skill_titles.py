"""backfill_skill_titles.py — fix Skill.title where it equals slug.

Symptom (validated 2026-05-19 on prod):
  ~16% of public skills have `title == slug` in the skills table. The carousel
  tagline backfill (sister script) fixed taglines, but the rendered card STILL
  shows "gh-fix-ci" as the headline because the card uses Skill.title.

Derivation priority:
  1. SKILL.md frontmatter top-level `title:` scalar (if present and != slug)
  2. SKILL.md frontmatter top-level `name:` scalar (if present and != slug)
  3. slug → Title Case with acronym preservation. Each hyphen-separated word
     either stays uppercase if it's a known acronym (LLM, CI, MCP, API, CLI,
     etc.) or is capitalized. CLI-tool-style slugs whose FIRST token is short
     and lowercase (e.g. `gh-fix-ci`, `npm-audit-watch`) are preserved as
     "<tool> <Verb> <Object>" with the tool name kept lowercase.

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
import re
import sys

from app.database import SessionLocal
from app.models import Skill

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Acronyms that should stay uppercase in title-case derivation. Lowercased on
# input. Order doesn't matter; this is a membership check.
_ACRONYMS = frozenset(
    {
        "ai",
        "ci",
        "cd",
        "cli",
        "api",
        "ui",
        "ux",
        "id",
        "os",
        "pr",
        "qa",
        "ml",
        "llm",
        "mcp",
        "rest",
        "sdk",
        "tts",
        "stt",
        "rag",
        "rl",
        "db",
        "fs",
        "gpu",
        "cpu",
        "io",
        "url",
        "uri",
        "http",
        "https",
        "json",
        "yaml",
        "xml",
        "js",
        "ts",
        "css",
        "html",
        "sql",
        "vps",
        "dns",
        "tls",
        "ssl",
        "ssh",
        "rss",
        "fyi",
        "ack",
        "pm",
        "wip",
        "tdd",
        "bdd",
        "dx",
    }
)

# Known CLI-tool prefixes. If slug starts with one of these followed by `-`,
# we preserve the tool name lowercase and treat the rest as the action.
_CLI_TOOLS = frozenset({"gh", "git", "npm", "pip", "uv", "pnpm", "yarn", "kubectl", "docker"})


def _parse_frontmatter_field(readme: str, field: str) -> str | None:
    """Regex-based frontmatter parser. Returns the value of `field` or None.

    PyYAML would refuse multi-line `unhappy_paths` blocks; a regex on a simple
    top-level scalar is robust enough for this backfill.
    """
    if not readme:
        return None
    m = _FRONTMATTER_RE.match(readme)
    if not m:
        return None
    block = m.group(1)
    line_re = re.compile(rf"^{re.escape(field)}:\s*(.+?)\s*$", re.MULTILINE)
    match = line_re.search(block)
    if not match:
        return None
    val = match.group(1).strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    return val or None


def _cap_word(w: str) -> str:
    """Capitalize a word, preserving acronyms."""
    if not w:
        return w
    return w.upper() if w.lower() in _ACRONYMS else w.capitalize()


def _slug_to_title(slug: str) -> str:
    """Hyphens → spaces, capitalize per word, but preserve known acronyms
    and known CLI tool prefixes."""
    parts = [p for p in slug.replace("_", "-").split("-") if p]
    if not parts:
        return slug

    out: list[str] = []
    for i, p in enumerate(parts):
        # First-token CLI-tool exception: keep lowercase tool name verbatim
        if i == 0 and p.lower() in _CLI_TOOLS and len(parts) >= 2:
            out.append(p.lower())
        else:
            out.append(_cap_word(p))
    return " ".join(out)


def derive_title(skill: Skill) -> str | None:
    """Return the proposed new title, or None if no improvement on the slug."""
    readme = getattr(skill, "readme", None) or ""

    fm_title = _parse_frontmatter_field(readme, "title")
    if fm_title and fm_title != skill.slug:
        return fm_title

    fm_name = _parse_frontmatter_field(readme, "name")
    if fm_name and fm_name != skill.slug:
        return fm_name

    proposed = _slug_to_title(skill.slug)
    if proposed and proposed != skill.slug:
        return proposed

    return None


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
            new_title = derive_title(s)
            if not new_title or new_title == s.title:
                skipped += 1
                continue
            # Determine source for log
            readme = getattr(s, "readme", None) or ""
            if _parse_frontmatter_field(readme, "title"):
                source = "frontmatter:title"
            elif (
                _parse_frontmatter_field(readme, "name")
                and _parse_frontmatter_field(readme, "name") != s.slug
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
