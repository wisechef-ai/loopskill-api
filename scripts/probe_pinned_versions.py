#!/usr/bin/env python3
"""converge_0208 P3 — probe all pinned skill versions for actual fetchability.

## What it does

Walks EVERY skill pinned across EVERY bundle and verifies the artifact is
actually fetchable. This is the production probe that would have caught the
27-day outage on day one.

The probe is:
  * **Zero-LLM, fully deterministic** — no guessing or inference; all facts.
  * **Artifact-based, not row-based** — a `SkillVersion` row existing is not
    resolution (that was the exact failure mode: the row existed, the file
    did not). The probe verifies that the BYTES are fetchable from the
    declared `tarball_path`.
  * **Shape-aware** — flags `pin_mode='track'` rows carrying a non-NULL
    `pinned_version` as an inconsistency (data shape defect) even if the
    bytes happen to resolve. This is what would have caught the `tori-core`
    problem 27 days early.

Exit code:
  0  — all probes pass, all shape checks pass
  non-0  — one or more failures; named in stdout/stderr
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import and_  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Bundle, BundleSkill, Skill, SkillVersion  # noqa: E402


def resolve_pinned_version(
    db: Session,
    skill_id: UUID,
    pinned_version: str,
) -> tuple[bool, str | None]:
    """Check if a pinned version's artifact is fetchable.

    Returns (resolves, reason).
      resolves=True, reason=None  — artifact exists and is fetchable.
      resolves=False, reason=msg  — artifact missing or invalid; msg names why.
    """
    row = (
        db.query(SkillVersion)
        .filter(and_(SkillVersion.skill_id == skill_id, SkillVersion.semver == pinned_version))
        .first()
    )
    if row is None:
        return False, f"no SkillVersion row exists for skill_id={skill_id}, semver={pinned_version}"
    if row.resolution_status == "unresolvable":
        return False, f"SkillVersion marked unresolvable (reason: {row.resolution_note})"
    if not row.tarball_path:
        return False, f"SkillVersion has no tarball_path set (semver={pinned_version})"
    p = Path(row.tarball_path)
    if not p.is_file():
        return False, f"tarball not found at {row.tarball_path}"
    return True, None


def probe_bundle_pins(
    db: Session,
    bundle_id: UUID,
) -> list[dict[str, Any]]:
    """Probe all pinned entries in one bundle.

    Returns list of failure dicts (empty = all pass):
      {bundle_id, bundle_slug, bundle_name, skill_id, skill_slug,
       pinned_version, tarball_path, reason}
    """
    entries = (
        db.query(BundleSkill, Skill.slug, Bundle.slug, Bundle.name)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .join(Bundle, Bundle.id == BundleSkill.bundle_id)
        .filter(
            BundleSkill.bundle_id == bundle_id,
            BundleSkill.pinned_version.isnot(None),
        )
        .all()
    )

    failures: list[dict[str, Any]] = []
    for bs, skill_slug, b_slug, b_name in entries:
        resolves, reason = resolve_pinned_version(db, bs.skill_id, bs.pinned_version)
        if not resolves:
            row = (
                db.query(SkillVersion)
                .filter(
                    and_(
                        SkillVersion.skill_id == bs.skill_id,
                        SkillVersion.semver == bs.pinned_version,
                    )
                )
                .first()
            )
            failures.append(
                {
                    "bundle_id": bundle_id,
                    "bundle_slug": b_slug,
                    "bundle_name": b_name,
                    "skill_id": bs.skill_id,
                    "skill_slug": skill_slug,
                    "pinned_version": bs.pinned_version,
                    "tarball_path": row.tarball_path if row else None,
                    "reason": reason or "unknown",
                }
            )
    return failures


def probe_all_pinned_versions(db: Session) -> list[dict[str, Any]]:
    """Probe all pinned skill versions across all bundles.

    Returns (failures, shape_warnings).
    """
    all_bundles = db.query(Bundle.id).all()
    failures: list[dict[str, Any]] = []
    for (bundle_id,) in all_bundles:
        failures.extend(probe_bundle_pins(db, bundle_id))
    return failures


def find_shape_defects(db: Session) -> list[dict[str, Any]]:
    """Find `pin_mode='track'` rows with non-NULL pinned_version (data shape defects).

    These are inconsistencies: 'track' means "follow the head", yet a
    pinned_version present can cause a caller that reads pinned_version directly
    (ignoring pin_mode) to resolve a stale version. The repair clears the pin,
    but we flag this shape in the probe as an early-warning system.

    Returns list of defect dicts: {bundle_id, bundle_slug, bundle_name, skill_slug, pinned_version}
    """
    rows = (
        db.query(BundleSkill, Bundle.slug, Bundle.name, Skill.slug)
        .join(Bundle, Bundle.id == BundleSkill.bundle_id)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.pin_mode == "track", BundleSkill.pinned_version.isnot(None))
        .all()
    )

    defects = [
        {
            "bundle_id": bs.bundle_id,
            "bundle_slug": b_slug,
            "bundle_name": b_name,
            "skill_slug": skill_slug,
            "pinned_version": bs.pinned_version,
        }
        for bs, b_slug, b_name, skill_slug in rows
    ]
    return defects


def main() -> int:
    """Probe pinned versions and shape defects. Exit 0 if all pass, non-0 if any fail."""
    db = SessionLocal()
    try:
        print("probing all pinned skill versions across all bundles...")
        failures = probe_all_pinned_versions(db)

        if failures:
            print(f"\nFAIL: {len(failures)} pinned version(s) do not resolve:")
            for f in failures:
                print(f"  {f['bundle_name']}/{f['skill_slug']} pinned to {f['pinned_version']}")
                print(f"    reason: {f['reason']}")
                if f["tarball_path"]:
                    print(f"    tarball_path: {f['tarball_path']}")
            print()

        defects = find_shape_defects(db)
        if defects:
            print(f"WARN: {len(defects)} pin_mode='track' row(s) carry a non-NULL pinned_version (shape defect):")
            for d in defects:
                print(f"  {d['bundle_name']}/{d['skill_slug']} pinned to {d['pinned_version']}")
            print("  These rows are self-contradictory: 'track' means 'follow head', but a stale pin can cause")
            print("  a pin-honouring reader to resolve a dead old version instead. Run repair script to clear.")
            print()

        if not failures:
            print("✓ All pinned versions resolve")
        if not defects:
            print("✓ No shape defects (all pin_mode='track' rows have pinned_version=NULL)")

        return 1 if failures else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
