#!/usr/bin/env python3
"""scripts/harvest_cookbook.py

Deterministic Python (NO LLM). Walks a skills directory, parses SKILL.md
frontmatter using python-frontmatter, scores each skill, and outputs a
ranked CSV.

Score formula:
    score = (1 / (recency_days + 1)) * audit_weight * cross_host_weight

where:
    audit_weight     = 2.0 if audit_pass else 1.0
    cross_host_weight = 1 + len(hosts) * 0.5  (1 host = 1.5, 3 hosts = 2.5)

Usage:
    python scripts/harvest_cookbook.py [--dir ~/.hermes/skills] [--out /tmp/harvest.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import frontmatter as fm
except ImportError:
    print("ERROR: python-frontmatter not installed. Run: pip install python-frontmatter", file=sys.stderr)
    sys.exit(1)


@dataclass
class SkillEntry:
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    tier: str = ""
    version: str = ""
    audit_pass: bool = False
    hosts: list[str] = field(default_factory=list)
    recency_days: Optional[float] = None
    score: float = 0.0
    source_path: str = ""


def parse_skill_md(path: Path) -> SkillEntry:
    """Parse a SKILL.md file and return a SkillEntry.

    Frontmatter fields:
        name, description, tags, tier, version, audit_pass, hosts
    """
    post = fm.load(str(path))
    meta = post.metadata

    mtime = path.stat().st_mtime
    recency_days = (time.time() - mtime) / 86400

    return SkillEntry(
        name=meta.get("name", path.parent.name),
        description=meta.get("description", ""),
        tags=list(meta.get("tags", []) or []),
        tier=meta.get("tier", ""),
        version=meta.get("version", ""),
        audit_pass=bool(meta.get("audit_pass", False)),
        hosts=list(meta.get("hosts", []) or []),
        recency_days=recency_days,
        source_path=str(path),
    )


def score_skill(entry: SkillEntry) -> float:
    """Compute deterministic score for a SkillEntry.

    Higher score = better candidate for the Cookbook.
    """
    recency = entry.recency_days if entry.recency_days is not None else 365
    audit_weight = 2.0 if entry.audit_pass else 1.0
    host_count = len(entry.hosts)
    cross_host_weight = 1.0 + host_count * 0.5
    return (1.0 / (recency + 1)) * audit_weight * cross_host_weight


def harvest_directory(skills_dir: str) -> list[SkillEntry]:
    """Walk skills_dir, parse all SKILL.md files, score and sort.

    Returns list of SkillEntry sorted by score descending.
    """
    root = Path(skills_dir)
    entries: list[SkillEntry] = []

    for skill_md in root.rglob("SKILL.md"):
        try:
            entry = parse_skill_md(skill_md)
            entry.score = score_skill(entry)
            entries.append(entry)
        except Exception as exc:
            print(f"WARN: skipping {skill_md}: {exc}", file=sys.stderr)

    entries.sort(key=lambda e: e.score, reverse=True)
    return entries


def write_csv(entries: list[SkillEntry], outpath: str) -> None:
    """Write ranked entries to a CSV file."""
    fieldnames = [
        "name", "description", "tier", "version", "audit_pass",
        "hosts", "recency_days", "score", "source_path",
    ]
    with open(outpath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for e in entries:
            writer.writerow({
                "name": e.name,
                "description": e.description,
                "tier": e.tier,
                "version": e.version,
                "audit_pass": e.audit_pass,
                "hosts": ",".join(e.hosts),
                "recency_days": f"{e.recency_days:.2f}" if e.recency_days is not None else "",
                "score": f"{e.score:.6f}",
                "source_path": e.source_path,
            })


def main() -> None:
    from datetime import datetime

    parser = argparse.ArgumentParser(description="Harvest skills for Cookbook curation")
    parser.add_argument(
        "--dir",
        default=os.path.expanduser("~/.hermes/skills"),
        help="Directory to scan for SKILL.md files",
    )
    parser.add_argument(
        "--out",
        default=f"/tmp/cookbook-harvest-{datetime.now().strftime('%Y-%m-%d')}.csv",
        help="Output CSV path",
    )
    args = parser.parse_args()

    print(f"Scanning {args.dir}...", file=sys.stderr)
    entries = harvest_directory(args.dir)
    print(f"Found {len(entries)} skills.", file=sys.stderr)

    write_csv(entries, args.out)
    print(f"Written to {args.out}", file=sys.stderr)

    # Print top 10 to stdout for quick review
    for i, e in enumerate(entries[:10], 1):
        print(f"{i:2d}. {e.name:<30s}  score={e.score:.4f}  audit={e.audit_pass}  hosts={e.hosts}")


if __name__ == "__main__":
    main()
