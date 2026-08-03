#!/usr/bin/env python3
"""converge_0208 P3 — repair `skill_versions` rows whose `tarball_path` is dead.

## Background

13 of 76 `skill_versions` rows on wisechef-hq carry a `tarball_path` under
`/storage/skills/...` — a path that does not exist after a storage migration.
Live artifacts now live at `/var/lib/recipes-skills/<slug>/<semver>.tar.gz`.

This hid for 27 days because human installs resolve `latest` (which always
has a live artifact); fleet reconcile resolves the raw PIN, which is the only
path that ever touches an OLD version — exactly the ones the migration
orphaned. See `/home/adam/.hermes/sprints/loopskill-0308/SHARED_CONTEXT.md`
§1 for the full verified ground truth.

## What this script does

For every `skill_versions` row whose `tarball_path` does not resolve to a
real file, in this strict order:

  1. **Repoint** — if the artifact exists at the canonical location
     (`<artifact_root>/<slug>/<semver>.tar.gz`), the row is repointed there.
     Same bytes, corrected path.
  2. **Mark unresolvable (superseded)** — the artifact is genuinely gone, but
     a NEWER version of the same skill resolves. The old version is marked
     `resolution_status='unresolvable'`. It is never silently repointed at a
     newer version's bytes — a pinned version's whole point is that it names
     an EXACT set of bytes; repointing 1.0.0 at 1.0.1 would lie about what the
     pin contains.
  3. **Mark unresolvable (gone)** — nothing resolves for this skill at all.
     Marked `resolution_status='unresolvable'` so a mint/reconcile consumer
     refuses the version loudly instead of installing broken bytes.

**No tarball is ever fabricated.** If the bytes are gone, the honest state is
`unresolvable` — a loud, inspectable failure is the feature, not a bug.

## A second, independent repair: stale `track` pins

Separately (`--fix-track-pins`), `bundle_skills` rows with `pin_mode='track'`
that still carry a non-NULL `pinned_version` are self-contradictory: 'track'
means "follow the head", yet a stale pin can cause callers that read
`pinned_version` directly (ignoring `pin_mode`) to resolve a dead old version.
This mode clears `pinned_version` on exactly those rows so they follow the
head, as 'track' promises.

## Usage

    # Dry run (default) — prints the plan, writes nothing.
    python scripts/repair_dead_skill_version_paths.py

    # Apply the tarball-path repair.
    python scripts/repair_dead_skill_version_paths.py --execute

    # Also (or only) repair stale track pins, optionally scoped to one bundle.
    python scripts/repair_dead_skill_version_paths.py --fix-track-pins --bundle-slug tori-core
    python scripts/repair_dead_skill_version_paths.py --fix-track-pins --bundle-slug tori-core --execute
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import or_  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Bundle, BundleSkill, Skill, SkillVersion  # noqa: E402
from app.services.semver import semver_key  # noqa: E402

DEFAULT_ARTIFACT_ROOT = Path("/var/lib/recipes-skills")


# ── Tarball-path repair ─────────────────────────────────────────────────


def canonical_path(artifact_root: Path, slug: str, semver: str) -> Path:
    """The one true location for a skill's tarball post-migration."""
    return artifact_root / slug / f"{semver}.tar.gz"


def _tarball_exists(path: str | None) -> bool:
    return bool(path) and Path(path).is_file()


def plan_repairs(db: Session, artifact_root: Path = DEFAULT_ARTIFACT_ROOT) -> list[dict[str, Any]]:
    """Compute the repair action for every dead-tarball `skill_versions` row.

    Returns one dict per DEAD row (rows that already resolve are omitted):
      {skill_id, slug, semver, old_path, action, new_path, new_status, reason}
    action is one of "repoint" | "mark_unresolvable".
    """
    rows = (
        db.query(SkillVersion, Skill.slug)
        .join(Skill, Skill.id == SkillVersion.skill_id)
        .order_by(Skill.slug, SkillVersion.semver)
        .all()
    )

    by_skill: dict[UUID, list[SkillVersion]] = {}
    for v, _slug in rows:
        by_skill.setdefault(v.skill_id, []).append(v)

    plans: list[dict[str, Any]] = []
    for v, slug in rows:
        if _tarball_exists(v.tarball_path):
            continue  # already resolves — nothing to do

        candidate = canonical_path(artifact_root, slug, v.semver)
        if candidate.is_file():
            plans.append(
                {
                    "skill_version_id": v.id,
                    "slug": slug,
                    "semver": v.semver,
                    "old_path": v.tarball_path,
                    "action": "repoint",
                    "new_path": str(candidate),
                    "new_status": "ok",
                    "reason": "artifact exists at the canonical path under a different path",
                }
            )
            continue

        newer_resolves = any(
            semver_key(other.semver) > semver_key(v.semver)
            and (
                _tarball_exists(other.tarball_path)
                or canonical_path(artifact_root, slug, other.semver).is_file()
            )
            for other in by_skill[v.skill_id]
        )
        if newer_resolves:
            reason = (
                "no artifact for this exact version; a newer version of this skill resolves — "
                "marking unresolvable rather than repointing at different bytes"
            )
        else:
            reason = "no artifact found anywhere for this skill/version"
        plans.append(
            {
                "skill_version_id": v.id,
                "slug": slug,
                "semver": v.semver,
                "old_path": v.tarball_path,
                "action": "mark_unresolvable",
                "new_path": v.tarball_path,
                "new_status": "unresolvable",
                "reason": reason,
            }
        )
    return plans


def apply_repairs(db: Session, plans: list[dict[str, Any]]) -> None:
    """Write the planned repairs. Caller commits."""
    for p in plans:
        row = db.query(SkillVersion).filter(SkillVersion.id == p["skill_version_id"]).first()
        if row is None:
            continue
        if p["action"] == "repoint":
            row.tarball_path = p["new_path"]
            row.resolution_status = "ok"
            row.resolution_note = None
        elif p["action"] == "mark_unresolvable":
            row.resolution_status = "unresolvable"
            row.resolution_note = p["reason"]


def print_repair_table(plans: list[dict[str, Any]]) -> None:
    if not plans:
        print("no dead skill_versions rows found — nothing to repair")
        return
    print(f"{'slug':<28}{'semver':<10}{'action':<20}{'old path':<45}new state")
    for p in plans:
        new_state = p["new_path"] if p["action"] == "repoint" else f"resolution_status={p['new_status']}"
        print(
            f"{p['slug']:<28}{p['semver']:<10}{p['action']:<20}{str(p['old_path']):<45}{new_state}"
        )


# ── Stale track-pin repair ──────────────────────────────────────────────


def plan_track_pin_repair(db: Session, bundle_slug: str | None = None) -> list[dict[str, Any]]:
    """Find `bundle_skills` rows with pin_mode='track' AND a non-NULL pinned_version.

    That combination is self-contradictory data: 'track' promises to follow
    the head, yet a stale pin can cause a pin-honouring reader to resolve a
    dead old version instead. Clearing pinned_version on these rows is the
    repair — it makes the row's behavior match what pin_mode already claims.
    """
    q = (
        db.query(BundleSkill, Bundle.name, Bundle.slug, Skill.slug)
        .join(Bundle, Bundle.id == BundleSkill.bundle_id)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.pin_mode == "track", BundleSkill.pinned_version.isnot(None))
    )
    if bundle_slug:
        q = q.filter(or_(Bundle.slug == bundle_slug, Bundle.name == bundle_slug))
    rows = q.order_by(Bundle.name, Skill.slug).all()

    return [
        {
            "bundle_skill_id": bs.id,
            "bundle_name": bundle_name,
            "bundle_slug": b_slug,
            "skill_slug": skill_slug,
            "old_pinned_version": bs.pinned_version,
        }
        for bs, bundle_name, b_slug, skill_slug in rows
    ]


def apply_track_pin_repair(db: Session, plans: list[dict[str, Any]]) -> None:
    """Clear pinned_version on the planned rows. Caller commits."""
    ids = [p["bundle_skill_id"] for p in plans]
    if not ids:
        return
    db.query(BundleSkill).filter(BundleSkill.id.in_(ids)).update(
        {"pinned_version": None}, synchronize_session=False
    )


def print_track_pin_table(plans: list[dict[str, Any]]) -> None:
    if not plans:
        print("no stale track pins found — nothing to repair")
        return
    print(f"{'bundle':<20}{'skill':<28}{'old pinned_version':<20}new state")
    for p in plans:
        bundle_label = p["bundle_slug"] or p["bundle_name"]
        print(f"{bundle_label:<20}{p['skill_slug']:<28}{p['old_pinned_version']:<20}pinned_version=NULL (follows head)")


# ── CLI ──────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument(
        "--artifact-root",
        default=str(DEFAULT_ARTIFACT_ROOT),
        help=f"canonical tarball root (default: {DEFAULT_ARTIFACT_ROOT})",
    )
    ap.add_argument(
        "--fix-track-pins",
        action="store_true",
        help="also (or only) clear stale pinned_version on pin_mode='track' bundle_skills rows",
    )
    ap.add_argument(
        "--track-pins-only",
        action="store_true",
        help="skip the tarball-path repair; only run --fix-track-pins",
    )
    ap.add_argument(
        "--bundle-slug",
        default=None,
        help="scope --fix-track-pins to one bundle (matched against slug or name)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    db = SessionLocal()
    try:
        if not args.track_pins_only:
            plans = plan_repairs(db, Path(args.artifact_root))
            print("== dead skill_versions.tarball_path repair ==")
            print_repair_table(plans)
            if args.execute:
                apply_repairs(db, plans)
                db.commit()
                print(f"applied {len(plans)} row(s)")
            else:
                print("DRY RUN — no changes written. Re-run with --execute to apply.")

        if args.fix_track_pins or args.track_pins_only:
            print()
            print("== stale pin_mode='track' pinned_version repair ==")
            track_plans = plan_track_pin_repair(db, bundle_slug=args.bundle_slug)
            print_track_pin_table(track_plans)
            if args.execute:
                apply_track_pin_repair(db, track_plans)
                db.commit()
                print(f"cleared {len(track_plans)} stale pin(s)")
            else:
                print("DRY RUN — no changes written. Re-run with --execute to apply.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
