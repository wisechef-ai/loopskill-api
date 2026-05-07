#!/usr/bin/env python3
"""Sync artifacts from recipes-api → recipes-skill.

This is the executable counterpart to .github/workflows/sync-recipes-skill.yml.
Runs in CI on every merge to main; mirrors selected files into the
customer-facing recipes-skill repo so docs/CLI never go stale.

Idempotent — running twice with no source change produces zero git diff.

Mirroring rules (kept narrow on purpose; expand only when needed):
  recipes-api/tools/recipes_cli.py         → recipes-skill/scripts/recipes-cli/recipes_cli.py
  recipes-api/docs/recipes-skill/README.md → recipes-skill/README.md
  recipes-api/docs/recipes-skill/SKILL.md  → recipes-skill/SKILL.md
  recipes-api/app/mcp/server.py            → recipes-skill/references/mcp_tools_reference.py  (read-only mirror)

Usage:
    python3 scripts/sync_recipes_skill.py --source-repo /path/to/recipes-api \\
        --target-repo /path/to/recipes-skill --commit-sha <sha>
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

MIRROR_RULES: list[tuple[str, str, str]] = [
    # (source_path, target_path, description)
    ("tools/recipes_cli.py", "scripts/recipes-cli/recipes_cli.py", "CLI binary"),
    ("docs/recipes-skill/README.md", "README.md", "Skill README"),
    ("docs/recipes-skill/SKILL.md", "SKILL.md", "Skill manifest"),
    ("docs/recipes-skill/QUICKSTART-publisher.md", "QUICKSTART-publisher.md", "Publisher quickstart"),
    ("docs/recipes-skill/QUICKSTART-subscriber.md", "QUICKSTART-subscriber.md", "Subscriber quickstart"),
    ("docs/recipes-skill/QUICKSTART-share.md", "QUICKSTART-share.md", "Cookbook share quickstart"),
    ("app/mcp/server.py", "references/mcp_tools_reference.py", "MCP tools reference (read-only mirror)"),
]


def _stamp_mirrored_file(target: Path, source_rel: str, sha: str) -> None:
    """Prepend an 'auto-mirrored from upstream' header to .md and .py files."""
    if not target.exists():
        return
    suffix = target.suffix.lower()
    content = target.read_text()
    marker = f"<!-- auto-mirrored from wisechef-ai/recipes-api:{source_rel} -->"
    if suffix in {".md"}:
        if marker in content:
            return  # already stamped
        header = f"{marker}\n<!-- DO NOT EDIT here — edit upstream and the bot will sync -->\n<!-- last sync: commit {sha[:7]} -->\n\n"
        target.write_text(header + content)
    elif suffix == ".py":
        py_marker = f"# auto-mirrored from wisechef-ai/recipes-api:{source_rel}"
        if py_marker in content:
            return
        header = (
            f"{py_marker}\n"
            f"# DO NOT EDIT here — edit upstream and the bot will sync\n"
            f"# last sync: commit {sha[:7]}\n"
        )
        # Preserve existing shebang if present
        if content.startswith("#!"):
            shebang, _, rest = content.partition("\n")
            target.write_text(f"{shebang}\n{header}\n{rest}")
        else:
            target.write_text(f"{header}\n{content}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--target-repo", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    args = parser.parse_args()

    if not args.source_repo.is_dir():
        print(f"ERR: source-repo not a dir: {args.source_repo}", file=sys.stderr)
        return 2
    if not args.target_repo.is_dir():
        print(f"ERR: target-repo not a dir: {args.target_repo}", file=sys.stderr)
        return 2

    mirrored = 0
    skipped = 0
    for source_rel, target_rel, description in MIRROR_RULES:
        src = args.source_repo / source_rel
        dst = args.target_repo / target_rel
        if not src.exists():
            print(f"  SKIP   {description:35s}  (source absent: {source_rel})")
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        _stamp_mirrored_file(dst, source_rel, args.commit_sha)
        print(f"  MIRROR {description:35s}  {source_rel} -> {target_rel}")
        mirrored += 1

    print(f"\nDone: {mirrored} mirrored, {skipped} skipped (source absent).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
