#!/usr/bin/env python3
"""audit_cookbook_cap.py — Cookbook-cap SSOT enforcement (loopclose_3005 Phase A).

The cookbook cap (Pro=10, Pro+=200, free=0) has exactly ONE home:
``config/tiers.yaml`` (key ``cookbook_limit`` per tier), read everywhere via
``app.tier_labels.cookbook_limit()``. Before this gate there were four
conflicting sources of the number (cookbook_routes ``>= 1``, auth_routes inline
map, recipes-marketing.yaml "Up to 20", and the plan's "10"). This script fails
CI if any hardcoded cookbook-cap literal reappears outside the SSOT.

What it flags (outside config/tiers.yaml):
  - ``max_cookbooks`` paired with a numeric literal (e.g. ``"max_cookbooks": 1``)
  - a literal cookbook-count bullet in marketing copy
    (e.g. ``N cookbook(s)`` / ``Up to N cookbooks`` with a digit, not a
    ``{placeholder}``)

What it does NOT flag:
  - ``cookbook_limit(...)`` helper calls (the correct SSOT read)
  - ``{pro_cookbooks}`` / ``{pro_plus_cookbooks}`` interpolation placeholders
  - the SSOT file itself, tests, migrations, and archival docs

Exit codes:
  0 — clean (no literal outside the SSOT)
  1 — a cookbook-cap literal leaked outside config/tiers.yaml

Usage:
  python3 scripts/audit_cookbook_cap.py            # scan from repo root
  python3 scripts/audit_cookbook_cap.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: max_cookbooks paired with a numeric literal — e.g.  "max_cookbooks": 1
#:   or  max_cookbooks=10 .  A bare ``max_cookbooks`` referencing a variable
#:   (``"max_cookbooks": limit``) is fine and is NOT matched (no digit).
MAX_COOKBOOKS_LITERAL = re.compile(r"max_cookbooks['\"]?\s*[:=]\s*\d+")

#: A literal cookbook-count phrase in copy — "1 cookbook", "Up to 20 cookbooks".
#: A ``{placeholder}`` immediately before "cookbook" is the correct, drift-proof
#: form and must NOT be flagged.
COOKBOOK_COUNT_LITERAL = re.compile(r"\b\d+\s+(?:personal\s+)?cookbooks?\b", re.IGNORECASE)

#: File extensions to scan.
SCAN_EXTENSIONS = {".py", ".md", ".yaml", ".yml", ".tsx", ".ts", ".astro"}

#: Exact relative paths ALLOWED to contain a cookbook-cap literal — the SSOT,
#: this script, and archival history.
SSOT_ALLOWLIST: set[str] = {
    "config/tiers.yaml",
    "scripts/audit_cookbook_cap.py",
    "CHANGELOG.md",
}

#: Directory PREFIXES to skip entirely (mirrors audit_tier_vocab.py).
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

#: Lines containing any of these markers are intentional (historical context,
#: per-cookbook API-key counts which are a DIFFERENT metric, etc.).
INLINE_ALLOWLIST_MARKERS: tuple[str, ...] = (
    "scoped API keys",  # "Per-cookbook scoped API keys (up to 20)" — key count, not cap
    "API keys (up to",
    "audit_cookbook_cap",  # self-reference
    "noqa: cookbook-cap",  # explicit per-line opt-out for a justified literal
)


def _is_skipped(rel_path: str) -> bool:
    return any(rel_path.startswith(p) for p in SKIP_DIR_PREFIXES)


def scan(root: Path) -> list[tuple[Path, int, str]]:
    """Return [(file, line_no, line_text), ...] for every cap-literal violation."""
    violations: list[tuple[Path, int, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_EXTENSIONS:
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SSOT_ALLOWLIST or _is_skipped(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if any(marker in line for marker in INLINE_ALLOWLIST_MARKERS):
                continue
            if MAX_COOKBOOKS_LITERAL.search(line):
                violations.append((path, i, line.strip()))
                continue
            # A hardcoded "N cookbooks" phrase is a violation ONLY when the line
            # carries no {placeholder} interpolation. A bullet like
            # "Up to {pro_cookbooks} cookbooks" is the correct drift-proof form.
            if "{" not in line and COOKBOOK_COUNT_LITERAL.search(line):
                violations.append((path, i, line.strip()))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Cookbook-cap SSOT enforcement.")
    parser.add_argument("--root", default=".", help="Repo root to scan (default: cwd).")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    violations = scan(root)

    if not violations:
        print("audit_cookbook_cap: clean — no cookbook-cap literal outside config/tiers.yaml.")
        return 0

    print("audit_cookbook_cap: FAIL — cookbook-cap literal found outside the SSOT (config/tiers.yaml):")
    for path, ln, text in violations:
        print(f"  {path.relative_to(root).as_posix()}:{ln}: {text}")
    print()
    print("Fix: read the cap via app.tier_labels.cookbook_limit() (code) or use the")
    print("{pro_cookbooks}/{pro_plus_cookbooks} placeholder (marketing copy). The only")
    print("place a cookbook-cap number may live is config/tiers.yaml.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
