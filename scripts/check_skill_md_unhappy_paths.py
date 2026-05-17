#!/usr/bin/env python3
"""scripts/check_skill_md_unhappy_paths.py — CI gate for unhappy_paths.

Used by .github/workflows/recipes-skill-md-content-check.yml.

Exit 0 if the given SKILL.md file has >=3 well-formed unhappy_paths entries
in its YAML frontmatter; exit 1 otherwise with a clear error message.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_skill_md_unhappy_paths.py <path/to/SKILL.md>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"❌ {path}: cannot read: {exc}")
        return 1

    if not text.startswith("---"):
        print(f"❌ {path}: missing YAML frontmatter (need unhappy_paths >=3)")
        return 1

    try:
        end = text.index("\n---", 3)
    except ValueError:
        print(f"❌ {path}: unterminated frontmatter")
        return 1

    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError as exc:
        print(f"❌ {path}: frontmatter YAML parse error: {exc}")
        return 1

    if not isinstance(fm, dict):
        print(f"❌ {path}: frontmatter is not a mapping")
        return 1

    ups = fm.get("unhappy_paths")
    if not isinstance(ups, list) or len(ups) < 3:
        n = len(ups) if isinstance(ups, list) else 0
        print(f"❌ {path}: needs >=3 unhappy_paths entries (have {n})")
        print("   Add to frontmatter:")
        print("   unhappy_paths:")
        print("     - condition: <specific failure mode>")
        print("       recovery: <concrete recovery action>")
        return 1

    for i, e in enumerate(ups):
        if not isinstance(e, dict) or "condition" not in e or "recovery" not in e:
            print(f"❌ {path}: entry {i} missing condition/recovery keys")
            return 1
        if not (e.get("condition") or "").strip() or not (e.get("recovery") or "").strip():
            print(f"❌ {path}: entry {i} has empty condition or recovery")
            return 1

    print(f"✅ {path}: {len(ups)} unhappy_paths entries OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
