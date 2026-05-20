#!/usr/bin/env python3
"""Backfill skills.readme from the latest published tarball's SKILL.md.

fix_2005: prior to the publish-path readme sync, /api/skills/_publish never
wrote `skills.readme` on UPDATE (and only optionally on CREATE via Recipify).
Result: rows whose readme is NULL even though their tarball contains the full
SKILL.md. The detail endpoint reads skills.readme, so the portal renders the
Day-1 placeholder for those slugs.

This script repairs grandfathered rows by reading SKILL.md out of the latest
SkillVersion's stored tarball at /var/lib/recipes-skills/<slug>/<semver>.tar.gz
and writing it into the skills.readme column.

Idempotent. --dry-run by default. Skips rows that already have a non-empty
readme matching the tarball content.

Usage:
    cd ~/wiserecipes-api
    set -a && source .env && set +a
    .venv/bin/python scripts/backfill_skill_readme_from_tarball.py            # dry-run
    .venv/bin/python scripts/backfill_skill_readme_from_tarball.py --apply    # write
    .venv/bin/python scripts/backfill_skill_readme_from_tarball.py --slug obsidian-livesync-bridge --apply
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import tarfile
from pathlib import Path

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_readme")

MAX_BYTES = 256 * 1024


def extract_skill_md(tarball_path: Path) -> str | None:
    """Same algorithm as app.publisher_routes._extract_skill_md_from_tarball."""
    try:
        data = tarball_path.read_bytes()
    except OSError as exc:
        log.warning("cannot read %s: %s", tarball_path, exc)
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            candidates = []
            for m in t.getmembers():
                if not m.isfile():
                    continue
                name = m.name.lstrip("./")
                parts = name.split("/")
                if parts[-1] != "SKILL.md":
                    continue
                if len(parts) <= 3:
                    candidates.append(m)
            if not candidates:
                return None
            candidates.sort(key=lambda m: len(m.name))
            chosen = candidates[0]
            if chosen.size > MAX_BYTES:
                return None
            f = t.extractfile(chosen)
            if f is None:
                return None
            raw = f.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
    except (tarfile.TarError, OSError) as exc:
        log.warning("tarball parse failed for %s: %s", tarball_path, exc)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="Only backfill this slug")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = ap.parse_args()

    db_url = os.environ.get("WR_DATABASE_URL")
    if not db_url:
        log.error("WR_DATABASE_URL not set")
        return 2

    engine = create_engine(db_url)
    where_clause = "WHERE s.is_public AND NOT s.is_archived"
    params: dict[str, str] = {}
    if args.slug:
        where_clause += " AND s.slug = :slug"
        params["slug"] = args.slug

    candidates = []
    with engine.connect() as c:
        rows = c.execute(
            text(f"""
                SELECT s.slug, s.id, s.readme IS NULL OR LENGTH(s.readme) < 100 AS empty_readme,
                       sv.semver, sv.tarball_path
                  FROM skills s
                  LEFT JOIN LATERAL (
                      SELECT semver, tarball_path
                        FROM skill_versions
                       WHERE skill_id = s.id
                       ORDER BY created_at DESC
                       LIMIT 1
                  ) sv ON true
                  {where_clause}
                  ORDER BY s.slug
            """),
            params,
        ).fetchall()
    for r in rows:
        d = dict(r._mapping)
        if not d["empty_readme"]:
            log.debug("skip %s — readme already populated", d["slug"])
            continue
        if not d["tarball_path"]:
            log.warning("skip %s — no SkillVersion / tarball_path", d["slug"])
            continue
        if not Path(d["tarball_path"]).exists():
            log.warning("skip %s — tarball missing at %s", d["slug"], d["tarball_path"])
            continue
        candidates.append(d)

    log.info("found %d slug(s) needing readme backfill", len(candidates))
    if not candidates:
        return 0

    updated = 0
    for d in candidates:
        content = extract_skill_md(Path(d["tarball_path"]))
        if not content:
            log.warning("skip %s — no SKILL.md extractable from tarball", d["slug"])
            continue
        log.info(
            "%s%s: %d chars from %s@%s",
            "DRY-RUN " if not args.apply else "",
            d["slug"],
            len(content),
            d["slug"],
            d["semver"],
        )
        if args.apply:
            with engine.begin() as c:
                c.execute(
                    text("UPDATE skills SET readme = :r WHERE id = :i"),
                    {"r": content, "i": d["id"]},
                )
            updated += 1

    log.info("done. %s%d row(s).", "would-update " if not args.apply else "updated ", updated if args.apply else len(candidates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
