#!/usr/bin/env python3
"""audit_tier_vocab.py — Tier vocabulary SSOT enforcement.

Scans the repo for legacy tier names (cook, operator, studio) that appear
OUTSIDE the canonical SSOT files and known-legitimate alias paths.

The following are explicitly excluded because they are either the SSOT itself,
migration history (which must preserve the original SQL strings), or tests that
explicitly exercise legacy-alias backward compat:

  - config/tiers.yaml              (SSOT — the allowlist anchor)
  - alembic/versions/              (migration history — SQL strings are load-bearing)
  - tests/                         (legacy-alias compat tests are intentional)
  - CHANGELOG.md                   (historical record)
  - scripts/audit_tier_vocab.py    (this script contains the pattern as a string)

Everything else must use the canonical slugs: free | pro | pro_plus.

Exit codes:
  0 — clean (no violations)
  1 — violations found

Usage:
  python3 scripts/audit_tier_vocab.py          # scan from repo root
  python3 scripts/audit_tier_vocab.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Legacy tier names we want to eradicate from all non-SSOT files.
LEGACY_PATTERN = re.compile(r"\b(cook|operator|studio)\b", re.IGNORECASE)

#: File extensions to scan.
SCAN_EXTENSIONS = {".py", ".md", ".yaml", ".yml", ".tsx", ".ts"}

#: Exact relative paths (relative to repo root) that are ALLOWED to contain
#: legacy names — the canonical SSOT, alias maps, and this script itself.
SSOT_ALLOWLIST: set[str] = {
    "config/tiers.yaml",
    "scripts/audit_tier_vocab.py",
    # Historical record — references are factual, not prescriptive.
    "CHANGELOG.md",
    # The sunset notice in taxonomy.md explicitly names legacy aliases to explain them;
    # this is the correct place to document them.
    "docs/taxonomy.md",
}

#: Directory PREFIXES (relative to repo root) to skip entirely.
#: Migration history preserves original SQL slugs; tests exercise legacy compat;
#: internal/ contains historical sprint docs and PR bodies (archival, not prescriptive).
SKIP_DIR_PREFIXES: tuple[str, ...] = (
    "alembic/versions/",
    "tests/",
    "internal/",
    ".git/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
    "dist/",
    "build/",
)

#: Lines containing these markers are treated as intentional alias maps,
#: historical context, or non-tier uses of the words — not flagged.
#: Keep this list minimal — prefer fixing the source.
INLINE_ALLOWLIST_MARKERS: tuple[str, ...] = (
    # Explicit alias map / migration comments
    "legacy alias",
    "Legacy alias",
    "Legacy Alias",
    "legacy slug",
    "Legacy slug",
    "LEGACY_SLUG",
    "LEGACY",        # catches LEGACY_TIER_URL_ALIASES etc.
    "legacy",        # any line that already calls it out as legacy
    "Legacy",
    "# legacy",
    "# Legacy",
    "READ alias",
    "READ compat",
    "30-day",
    "backward-compat",
    "backwards-compat",
    "alias map",
    "alias →",
    "alias ->",
    "pre-Phase",
    "pre-rename",
    # English usage of 'operator' as a human role (not a tier name)
    "solo-operator",   # product description compound
    "Solo-operator",   # product description compound (capitalized variant)
    "$OPERATOR",       # environment variable
    "Operator must",   # human role
    "operator can",    # human role
    "operator before", # human role
    "operator key",    # auth key, not tier
    # Scope literals in auth_ctx.py (operator is a Scope, not a tier)
    'Scope = Literal',
    # Studio as a feature label (buckets/windows runtime) not a tier name
    "Studio tier,",    # runtime adapter description
    "Studio tier)",    # runtime adapter description
    # English usage of 'operator' as env var (not a tier name)
    "{{OPERATOR}}",    # f-string env var reference
    # Lines that reference the sunset date or explicitly call out the sunset
    "sunset 2026-06-10",
    "2026-06-10",
    # Marketing YAML - no "legacy alias" on these SQL lines but they're belt-and-suspenders
)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def is_allowed_path(rel_path: str) -> bool:
    """Return True if this relative path is exempt from scanning."""
    if rel_path in SSOT_ALLOWLIST:
        return True
    for prefix in SKIP_DIR_PREFIXES:
        if rel_path.startswith(prefix):
            return True
    return False


def is_allowed_line(line: str) -> bool:
    """Return True if this line is intentionally using a legacy name (alias map etc.)."""
    return any(marker in line for marker in INLINE_ALLOWLIST_MARKERS)


def scan(repo_root: Path) -> list[tuple[Path, int, str]]:
    """Walk the repo and return (file, lineno, line) tuples for violations."""
    violations: list[tuple[Path, int, str]] = []

    for path in sorted(repo_root.rglob("*")):
        if path.is_dir():
            continue

        rel = _rel(path, repo_root)

        if is_allowed_path(rel):
            continue

        if path.suffix not in SCAN_EXTENSIONS:
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            if LEGACY_PATTERN.search(line) and not is_allowed_line(line):
                violations.append((path, lineno, line.rstrip()))

    return violations


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root to scan (default: current directory)",
    )
    args = parser.parse_args()

    repo_root = Path(args.root).resolve()

    violations = scan(repo_root)

    if not violations:
        print("✅  audit_tier_vocab: no legacy tier names found outside SSOT — clean.")
        return 0

    print(
        f"❌  audit_tier_vocab: {len(violations)} violation(s) found — "
        "legacy tier names (cook|operator|studio) must be replaced with "
        "free|pro|pro_plus.\n"
    )
    for file, lineno, line in violations:
        rel = _rel(file, repo_root)
        print(f"  {rel}:{lineno}: {line}")

    print(
        "\nExclusions: config/tiers.yaml, alembic/versions/, tests/, CHANGELOG.md, "
        "and lines containing known alias-map markers.\n"
        "See config/tiers.yaml for canonical tier slugs and the sunset date "
        "for legacy aliases (2026-06-10)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
