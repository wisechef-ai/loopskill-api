"""converge_0208 P1 — mint revision 1 for every bundle that has never been locked.

``BundleLock`` shipped as "THE core drift-killer primitive" and then sat at zero
rows in production for its entire life: nothing ever minted one. P1 makes
reconcile resolve THROUGH the lock, so every existing bundle needs a revision 1.
Reconcile mints lazily on read, but doing it deliberately — and seeing the
verdicts first — beats discovering them one agent poll at a time.

DRY RUN BY DEFAULT. ``--execute`` is required to write anything.

Per bundle the report is one of:

  MINT      would mint revision 1 (or does, under --execute)
  LOCKED    already has a lock; nothing to do
  EMPTY     declares nothing installable; nothing to freeze
  REFUSE    an entry has no resolvable artifact — the blocking slug is named,
            and NOTHING is written for that bundle

A REFUSE is not a script failure. It is the phase working: the bundle contains
an entry that no member could install, and the fix is to repair the data (P3),
not to freeze the breakage into a lock.

The report also carries advisories that do not block a mint but are worth a
human's attention:

  stale-pin   a declared row's ``pinned_version`` names a version whose artifact
              does not resolve. Harmless once resolution goes through the lock
              (a 'track' row ignores it), but it is the residue that caused the
              production rollback storm and P3 is repairing it.
  no-locator  a version row with no ``tarball_path`` at all. ``install_routes.
              _download`` 404s on these exactly as it does on a dangling path,
              so they are unservable — but a NULL locator is not evidence of a
              dangling pointer (federated publishes leave it NULL), so the mint
              predicate does not refuse them. Audit separately.

Usage::

    python -m scripts.backfill_bundle_locks                  # dry run, all bundles
    python -m scripts.backfill_bundle_locks --bundle <uuid>  # dry run, one bundle
    python -m scripts.backfill_bundle_locks --execute        # write revision 1
"""

from __future__ import annotations

import argparse
import sys
from typing import Any
from uuid import UUID

from app.database import SessionLocal
from app.models import Bundle, BundleSkill, Skill, SkillVersion
from app.services import artifact_resolution
from app.services.bundle_lock_sync import sync_bundle_lock
from app.services.drift_service import LockMintError, current_lock, resolve_bundle_entries


def _advisories(db, bundle: Bundle) -> list[str]:
    """Non-blocking findings on a bundle's declared rows."""
    out: list[str] = []
    rows = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == bundle.id, BundleSkill.source != "disabled")
        .all()
    )
    for bs, skill in rows:
        if bs.pinned_version:
            pinned = (
                db.query(SkillVersion)
                .filter(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.semver == bs.pinned_version,
                )
                .first()
            )
            reason = artifact_resolution.unresolvable_reason(
                db, skill=skill, semver=bs.pinned_version, version_row=pinned
            )
            if reason is not None:
                out.append(
                    f"stale-pin  {skill.slug} pinned_version={bs.pinned_version} "
                    f"(pin_mode={bs.pin_mode}) — {reason}"
                )
        versions = db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).all()
        for v in versions:
            if not v.tarball_path:
                out.append(f"no-locator {skill.slug} {v.semver} — tarball_path is NULL")
    return out


def _inspect(db, bundle: Bundle) -> dict[str, Any]:
    """Resolve one bundle and return its verdict without writing anything."""
    if current_lock(db, bundle.id) is not None:
        return {"verdict": "LOCKED", "detail": "", "advisories": []}

    advisories = _advisories(db, bundle)
    try:
        entries = resolve_bundle_entries(db, bundle.id, strict=True)
    except LockMintError as exc:
        return {
            "verdict": "REFUSE",
            "detail": str(exc),
            "blocking_slug": exc.slug,
            "advisories": advisories,
        }

    if not entries:
        return {"verdict": "EMPTY", "detail": "no installable entries", "advisories": advisories}

    listed = ", ".join(f"{e['slug']}@{e['version']}" for e in entries)
    return {"verdict": "MINT", "detail": f"{len(entries)} entries: {listed}", "advisories": advisories}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true", help="write revision 1 (default: dry run)")
    ap.add_argument("--bundle", default=None, help="limit to one bundle id")
    args = ap.parse_args(argv)

    db = SessionLocal()
    counts = {"MINT": 0, "LOCKED": 0, "EMPTY": 0, "REFUSE": 0}
    try:
        q = db.query(Bundle).order_by(Bundle.created_at)
        if args.bundle:
            try:
                bundle_id = UUID(args.bundle)
            except ValueError:
                print(f"not a bundle id: {args.bundle}", file=sys.stderr)
                return 2
            q = q.filter(Bundle.id == bundle_id)
        bundles = q.all()

        mode = "EXECUTE" if args.execute else "DRY RUN (no writes — pass --execute to mint)"
        print(f"backfill_bundle_locks — {mode}")
        print(f"{len(bundles)} bundle(s)\n")

        for bundle in bundles:
            report = _inspect(db, bundle)
            verdict = report["verdict"]
            counts[verdict] += 1
            print(f"[{verdict:6}] {bundle.name} ({bundle.id})")
            if report["detail"]:
                print(f"           {report['detail']}")
            for advisory in report["advisories"]:
                print(f"           ! {advisory}")

            if args.execute and verdict == "MINT":
                lock = sync_bundle_lock(db, bundle)
                db.commit()
                print(f"           -> minted revision {lock.revision} ({lock.lock_hash[:12]}…)")

        print(
            f"\nsummary: {counts['MINT']} to mint · {counts['LOCKED']} already locked · "
            f"{counts['EMPTY']} empty · {counts['REFUSE']} refused"
        )
        if counts["REFUSE"]:
            print(
                "\nA REFUSE means the bundle contains an entry no member could install.\n"
                "Repair the underlying skill_versions row, then re-run. Freezing it into\n"
                "a lock would ship the 404 to every member of that bundle."
            )
    finally:
        db.close()

    # Refusals are reported, not fatal: one pass should show the whole picture
    # rather than aborting on the first broken bundle.
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
