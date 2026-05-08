#!/usr/bin/env python
"""RCP-13: Backfill ``Skill.install_count`` from ``telemetry_events``.

The denormalised counter on ``skills.install_count`` was never wired to
the ``POST /api/telemetry`` ingestion path, so every public skill reads 0
in production despite hundreds of install events in ``telemetry_events``.

This script reconciles the counter to the source of truth (telemetry):

    UPDATE skills
       SET install_count = (
             SELECT count(*) FROM telemetry_events te
              WHERE te.skill_slug = skills.slug
                AND te.event_type = 'install'
           )

Safety properties:
- **Idempotent.** Running twice yields identical counts; the second run is a
  no-op for every row whose count already matches telemetry.
- **Per-skill report.** Prints a table of every change (slug, before, after)
  so the operator can sanity-check before/after.
- **Dry-run first.** Default mode prints what *would* change; pass --apply
  to actually write.
- **Single transaction.** Either every row commits or none does.

Usage:
    # Inspect what would change (read-only)
    python scripts/backfill_install_count.py

    # Actually apply the backfill
    python scripts/backfill_install_count.py --apply

    # Constrain to a specific skill (debug)
    python scripts/backfill_install_count.py --slug web-scraper-pro --apply
"""
from __future__ import annotations

import argparse
import sys
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import InstallEvent, Skill, TelemetryEvent


def compute_truth(db: Session, slug_filter: str | None = None) -> dict[str, int]:
    """Return {slug: actual_install_count} from the union of both install sources.

    Two tables record installs in the marketplace:
      - ``install_events``  — written by ``POST /api/skills/install`` (the
        canonical "I installed this skill" tarball-fetch endpoint).
      - ``telemetry_events`` — written by ``POST /api/telemetry`` with
        ``event_type='install'`` (richer client-side telemetry: agent
        class, version, etc).

    The denormalised ``Skill.install_count`` counter must reflect both.
    Most rows live in only one of the two tables (different time windows),
    so the truth count is the SUM of per-table counts grouped by slug.
    """
    truth: dict[str, int] = {}

    tele_q = (
        db.query(
            TelemetryEvent.skill_slug,
            func.count(TelemetryEvent.id).label("n"),
        )
        .filter(
            TelemetryEvent.event_type == "install",
            TelemetryEvent.skill_slug.isnot(None),
        )
        .group_by(TelemetryEvent.skill_slug)
    )
    if slug_filter:
        tele_q = tele_q.filter(TelemetryEvent.skill_slug == slug_filter)
    for slug, n in tele_q.all():
        truth[slug] = truth.get(slug, 0) + int(n or 0)

    install_q = (
        db.query(
            InstallEvent.skill_slug,
            func.count(InstallEvent.id).label("n"),
        )
        .filter(InstallEvent.skill_slug.isnot(None))
        .group_by(InstallEvent.skill_slug)
    )
    if slug_filter:
        install_q = install_q.filter(InstallEvent.skill_slug == slug_filter)
    for slug, n in install_q.all():
        truth[slug] = truth.get(slug, 0) + int(n or 0)

    return truth


def collect_diff(
    db: Session, slug_filter: str | None = None
) -> list[tuple[str, int, int]]:
    """Return [(slug, before, after), ...] for skills whose counter is wrong.

    Includes both ``before > after`` (counter stale-high after telemetry GC)
    and the common ``before < after`` (counter never incremented). Skills
    whose counter already matches telemetry are filtered out — that is what
    makes the script idempotent.
    """
    truth = compute_truth(db, slug_filter)

    skill_q = db.query(Skill)
    if slug_filter:
        skill_q = skill_q.filter(Skill.slug == slug_filter)
    skills: Iterable[Skill] = skill_q.all()

    rows: list[tuple[str, int, int]] = []
    for skill in skills:
        actual = truth.get(skill.slug, 0)
        current = int(skill.install_count or 0)
        if actual != current:
            rows.append((skill.slug, current, actual))
    return rows


def apply_diff(db: Session, diff: list[tuple[str, int, int]]) -> int:
    """Write the new counts. Returns the number of rows updated."""
    updated = 0
    for slug, _before, after in diff:
        n = (
            db.query(Skill)
            .filter(Skill.slug == slug)
            .update({Skill.install_count: after}, synchronize_session=False)
        )
        updated += n
    db.commit()
    return updated


def render_table(diff: list[tuple[str, int, int]]) -> str:
    if not diff:
        return "(no rows out of sync — counter already matches telemetry)"
    width = max(len(s) for s, _, _ in diff)
    width = max(width, len("slug"))
    header = f"{'slug':<{width}} | {'before':>7} | {'after':>7} | delta"
    sep = "-" * len(header)
    lines = [header, sep]
    for slug, before, after in sorted(diff, key=lambda r: -r[2]):
        delta = after - before
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"{slug:<{width}} | {before:>7} | {after:>7} | {sign}{delta}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (default: dry-run, print only).",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="Constrain the backfill to a single skill slug (debug).",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        diff = collect_diff(db, args.slug)

        print(render_table(diff))
        print()

        if not diff:
            print("Nothing to do. Counter is in sync with telemetry.")
            return 0

        if not args.apply:
            print(
                f"Dry-run: {len(diff)} skill(s) would be updated. "
                "Re-run with --apply to write."
            )
            return 0

        n = apply_diff(db, diff)
        print(f"Applied: {n} skill row(s) updated.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
