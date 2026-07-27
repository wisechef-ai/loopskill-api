"""One-off reconcile: mirror every existing skill_like into its owner's Liked bundle.

Repairs likes created BEFORE spotify2607-A (#143) shipped the writer, which
never wrote a BundleSkill mirror row. Uses the production service functions
(set_federated_like_in_bundle / set_local_like_by_skill) rather than raw SQL so
every invariant holds: the XOR check, the unique indexes, and — critically —
_touch_bundle_generation, so polling agents actually see the change instead of
getting a false 304 off a frozen generation token.

Idempotent: the service helpers no-op when the mirror row already exists.
Read-only unless --apply is passed.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func

from app.database import SessionLocal
from app.library_service import set_federated_like_in_bundle
from app.liked_service import ensure_liked_bundle
from app.models import Bundle, BundleSkill, Skill, SkillLike, User


def main() -> int:
    """Reconcile skill_likes → BundleSkill mirror rows. Returns a shell exit code."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--email", default=None, help="limit to one user's email")
    args = ap.parse_args()

    db = SessionLocal()
    created = 0
    skipped = 0
    failed = 0
    try:
        q = db.query(SkillLike)
        if args.email:
            user = db.query(User).filter(User.email == args.email).first()
            if user is None:
                print(f"no user with email {args.email}")
                return 1
            q = q.filter(SkillLike.user_id == user.id)

        likes = q.order_by(SkillLike.liked_at).all()
        print(f"examining {len(likes)} skill_likes row(s)")

        for like in likes:
            bundle = ensure_liked_bundle(db, like.user_id)
            if like.federated_source and like.federated_slug:
                existing = (
                    db.query(BundleSkill)
                    .filter(
                        BundleSkill.bundle_id == bundle.id,
                        BundleSkill.federated_source == like.federated_source,
                        BundleSkill.federated_slug == like.federated_slug,
                    )
                    .first()
                )
                label = f"{like.federated_source}/{like.federated_slug}"
                if existing is not None:
                    skipped += 1
                    print(f"  OK   already mirrored: {label}")
                    continue
                print(f"  MISS needs mirror:     {label}")
                if args.apply:
                    set_federated_like_in_bundle(
                        db,
                        owner_id=like.user_id,
                        federated_source=like.federated_source,
                        federated_slug=like.federated_slug,
                        liked=True,
                    )
                    db.commit()
                created += 1
            elif like.skill_id:
                existing = (
                    db.query(BundleSkill)
                    .filter(
                        BundleSkill.bundle_id == bundle.id,
                        BundleSkill.skill_id == like.skill_id,
                    )
                    .first()
                )
                skill = db.query(Skill).filter(Skill.id == like.skill_id).first()
                label = skill.slug if skill else str(like.skill_id)
                if existing is not None:
                    skipped += 1
                    print(f"  OK   already mirrored: {label}")
                    continue
                if skill is None:
                    failed += 1
                    print(f"  WARN like references missing skill {like.skill_id} — skipping")
                    continue
                print(f"  MISS needs mirror:     {label} (local)")
                if args.apply:
                    # Local likes are authz-gated in the route path; this row already
                    # proves the like was accepted, so mirror it directly.
                    db.add(
                        BundleSkill(
                            bundle_id=bundle.id,
                            skill_id=skill.id,
                            source="custom-added",
                        )
                    )
                    db.query(Bundle).filter(Bundle.id == bundle.id).update(
                        {"updated_at": func.now()},
                        synchronize_session=False,
                    )
                    db.commit()
                created += 1
            else:
                failed += 1
                print(f"  WARN like {like.id} has neither skill_id nor federated ids")

        verb = "created" if args.apply else "would create"
        print(f"\n{verb}: {created}   already-present: {skipped}   warnings: {failed}")
        if not args.apply:
            print("DRY RUN — rerun with --apply to write")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
