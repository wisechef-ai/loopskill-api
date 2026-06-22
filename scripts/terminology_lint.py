#!/usr/bin/env python3
"""terminology-lint — LoopSkill rename enforcement gate (Phase 0 keystone).

Fails the build if a banned kitchen noun reappears in NEW code/routes/UI.
Source of truth: TERMINOLOGY.md. Allowlist rules mirror the "Lint gate" section.

Usage:
    python scripts/terminology_lint.py            # scan, exit 1 on violation
    python scripts/terminology_lint.py --self-test  # run built-in assertions
    python scripts/terminology_lint.py --paths app src   # restrict scan roots

A line is a VIOLATION when it contains a banned noun AND is not allowlisted.
Banned nouns: cookbook, recipe, chef (brand sense).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Banned nouns (word-ish boundaries; case-insensitive).
BANNED = [
    re.compile(r"cookbook", re.IGNORECASE),
    re.compile(r"\brecipe", re.IGNORECASE),  # \b so "recipify"... still flagged; recipes_sync etc caught
    re.compile(r"\bchef\b", re.IGNORECASE),
]

# Default scan roots — code + UI surfaces only.
DEFAULT_ROOTS = ["app", "src"]

# Path globs that are NEVER scanned (allowlist rule 1, 4).
SKIP_DIR_PARTS = {
    ".git", "node_modules", "dist", ".pytest_cache", ".venv", "__pycache__",
    "alembic/versions",  # migration filenames keep historical names (rule 1)
}
SKIP_FILE_NAMES = {
    "TERMINOLOGY.md", "ARCHITECTURE.md", "CHANGELOG.md",
}
SKIP_PATH_SUBSTR = ["docs/migration/"]

# The Chef-AGENT protected sense (allowlist rule 2): a line mentioning chef is
# OK if it also references the fleet/agent context.
AGENT_CONTEXT = re.compile(
    r"\b(agent|fleet|sister|soul|agent-sync|wise-agents|cron|heartbeat|tori|wise)\b",
    re.IGNORECASE,
)

# Intentional compat markers (allowlist rules 3, 5).
COMPAT_MARKERS = ("# compat-alias", "# compat-test", "compat-alias", "compat-test")


def _skip_path(path: Path) -> bool:
    parts = path.as_posix()
    if any(part in SKIP_DIR_PARTS for part in path.parts):
        return True
    if "alembic/versions" in parts:
        return True
    if path.name in SKIP_FILE_NAMES:
        return True
    if any(s in parts for s in SKIP_PATH_SUBSTR):
        return True
    return False


def _line_allowlisted(line: str, banned_re: re.Pattern) -> bool:
    """A banned line is allowed iff a compat marker is present, OR the only
    banned hit is `chef` in an agent context."""
    if any(m in line for m in COMPAT_MARKERS):
        return True
    # If the ONLY banned term on this line is `chef` and it's agent-context, allow.
    has_cookbook = BANNED[0].search(line)
    has_recipe = BANNED[1].search(line)
    has_chef = BANNED[2].search(line)
    if has_chef and not has_cookbook and not has_recipe:
        if AGENT_CONTEXT.search(line):
            return True
    return False


def scan(roots: list[str]) -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".astro", ".vue", ".svelte",
            ".html", ".json", ".yml", ".yaml"}
    for root in roots:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in exts:
                continue
            if _skip_path(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                hit = next((b for b in BANNED if b.search(line)), None)
                if hit is None:
                    continue
                if _line_allowlisted(line, hit):
                    continue
                rel = path.relative_to(REPO_ROOT).as_posix()
                violations.append((rel, i, line.strip()[:120]))
    return violations


def self_test() -> int:
    """Built-in assertions — the gate must catch the right things and only those."""
    cases = [
        # (line, expect_violation)
        ("cookbook_id = bundle.id", True),
        ("class Recipe(Base):", True),
        ("from app.cookbook_routes import router", True),
        ("# compat-alias: /api/cookbooks -> /api/bundles", False),
        ("assert resp.json()['cookbook_id']  # compat-test", False),
        ("Chef pings the fleet via agent-sync", False),   # agent sense
        ("the Chef brand logo", True),                     # brand sense -> flagged
        ("bundle_id = bundles.c.id", False),
        ("loop registry endpoint", False),
        ("personality deploy", False),
        ("ping the sister agent Chef on heartbeat", False),
    ]
    failures = 0
    for line, expect in cases:
        hit = next((b for b in BANNED if b.search(line)), None)
        flagged = hit is not None and not _line_allowlisted(line, hit)
        ok = flagged == expect
        status = "ok " if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"  [{status}] expect_violation={expect!s:5} got={flagged!s:5} | {line}")
    print(f"\nself-test: {len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def _changed_lines(base_ref: str) -> dict[str, set[int]]:
    """Map of relpath -> set of ADDED line numbers in the diff vs base_ref.

    Diff mode lets the gate enforce the rename progressively: it only flags
    banned nouns on lines a PR ADDS, so the ~2455 legacy refs in the existing
    tree don't make the baseline red. Each renamed phase keeps shrinking them.
    """
    import subprocess

    out = subprocess.run(
        ["git", "diff", "--unified=0", f"{base_ref}...HEAD"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout
    changed: dict[str, set[int]] = {}
    cur: str | None = None
    new_ln = 0
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            changed.setdefault(cur, set())
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                new_ln = int(m.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            if cur is not None:
                changed[cur].add(new_ln)
            new_ln += 1
        elif not line.startswith("-"):
            new_ln += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--paths", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument(
        "--diff-base",
        help="Only flag banned nouns on lines added vs this git ref "
        "(progressive-enforcement mode for PRs). E.g. --diff-base origin/main",
    )
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    violations = scan(args.paths)

    if args.diff_base:
        changed = _changed_lines(args.diff_base)
        violations = [
            (rel, ln, snip)
            for (rel, ln, snip) in violations
            if ln in changed.get(rel, set())
        ]
        if not violations:
            print(
                f"terminology-lint (diff vs {args.diff_base}): "
                "no banned nouns in added lines"
            )
            return 0
    if violations:
        print(f"terminology-lint: {len(violations)} banned-noun violation(s):\n")
        for rel, ln, snippet in violations:
            print(f"  {rel}:{ln}: {snippet}")
        print("\nFix: rename to LoopSkill vocabulary (see TERMINOLOGY.md), "
              "or tag an intentional compat line with '# compat-alias' / '# compat-test'.")
        return 1
    print(f"terminology-lint: clean across {args.paths}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
