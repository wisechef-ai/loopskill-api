#!/usr/bin/env python
"""spotify_2607 Phase A — one-shot backfill: place orphaned federated likes
into the deployable Liked bundle without user action.

Adam's user id (ea6de87a-d34c-4fa0-b0a3-90723878e9d3) has exactly 2 likes in
``skill_likes``, BOTH federated, and his Liked bundle contains 0 skills — the
exact bug this sprint fixes. The mirror shipped in engagement_routes now lands
a federated like in BundleSkill on every NEW like, but pre-existing rows need
this one-shot to repair without waiting for Adam to re-like.

Usage:
    # Dry-run (default) — prints what WOULD be written, writes nothing.
    python scripts/spotify_2607_a_backfill_liked_federated.py

    # Commit the repair.
    python scripts/spotify_2607_a_backfill_liked_federated.py --commit

Idempotent: re-running is a no-op (the mirror is add-only; a row that already
exists is skipped). Resumable: if interrupted, re-run picks up where it left
off (each user+slug pair is independent).

Checkpointing: if this script cached anything expensive it would checkpoint
under ~/.hermes/state/ — NEVER /tmp (wiped on reboot; cost 78 minutes of work
on 2026-07-26). This script does no expensive caching (one indexed SELECT per
user, one idempotent mirror call per orphaned like), so no checkpoint is needed.

Runs against whatever DATABASE_URL points at. DO NOT point this at prod without
--commit AND a confirmed prod backup. The acceptance gate runs it against a
local/test DB only.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from uuid import UUID

logger = logging.getLogger("spotify_2607_a_backfill")

# If the app module isn't importable (wrong cwd), fail clearly.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.database import SessionLocal  # noqa: E402
from app.library_service import set_federated_like_in_bundle  # noqa: E402
from app.models import SkillLike  # noqa: E402

logger.info = lambda *a, **kw: None  # quiet the default logger unless --verbose


def _load_skill_likes_federated(db) -> list[tuple[UUID, str, str]]:
    """Return [(user_id, federated_source, federated_slug)] for every
    federated like in skill_likes (skill_id NULL, federated identity set).
    """
    rows = (
        db.query(SkillLike.user_id, SkillLike.federated_source, SkillLike.federated_slug)
        .filter(
            SkillLike.skill_id.is_(None),
            SkillLike.federated_source.isnot(None),
            SkillLike.federated_slug.isnot(None),
        )
        .all()
    )
    return [(uid, src, slug) for uid, src, slug in rows]


def backfill(*, commit: bool = False, verbose: bool = False) -> dict:
    """Run the backfill. Returns a summary dict."""
    db = SessionLocal()
    try:
        orphans = _load_skill_likes_federated(db)
        print(f"[backfill] found {len(orphans)} federated like(s) to repair")

        repaired = 0
        skipped = 0
        for user_id, source, slug in orphans:
            # The mirror is idempotent — it will skip if a BundleSkill row
            # already exists. We call it unconditionally so the summary is
            # honest about what changed.
            from app.models import BundleSkill
            from app.liked_service import ensure_liked_bundle

            bundle = ensure_liked_bundle(db, user_id)
            existing = (
                db.query(BundleSkill)
                .filter(
                    BundleSkill.bundle_id == bundle.id,
                    BundleSkill.federated_source == source,
                    BundleSkill.federated_slug == slug,
                )
                .first()
            )
            if existing is not None:
                skipped += 1
                if verbose:
                    print(f"  [skip] {user_id} {source}/{slug} — already in bundle")
                continue

            set_federated_like_in_bundle(
                db,
                owner_id=user_id,
                federated_source=source,
                federated_slug=slug,
                liked=True,
            )
            repaired += 1
            if verbose:
                print(f"  [repair] {user_id} {source}/{slug}")

        if commit:
            db.commit()
            print(f"[backfill] COMMIT — {repaired} row(s) written, {skipped} skipped")
        else:
            db.rollback()
            print(f"[backfill] DRY-RUN — would write {repaired} row(s), skip {skipped}")

        return {"found": len(orphans), "repaired": repaired, "skipped": skipped, "committed": commit}
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--commit", action="store_true", help="Write the repair (default: dry-run)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    result = backfill(commit=args.commit, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
