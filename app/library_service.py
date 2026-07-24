"""Liked-library reads and mutations shared by HTTP and MCP surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.bundle_routes import _touch_bundle_generation
from app.liked_service import ensure_liked_bundle
from app.models import (
    BundleCompositeLoop,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    FollowedBundle,
    Personality,
    Skill,
    SkillLike,
    User,
)

if TYPE_CHECKING:
    from app.auth_ctx import AuthContext

ArtifactType = Literal["skill", "personality", "loop"]


class LikedArtifactNotFoundError(Exception):
    """Raised when a requested liked-library artifact does not exist."""


class LikedArtifactForbiddenError(Exception):
    """Raised when the caller may not read the artifact they tried to like."""


def _artifact_spec(artifact_type: ArtifactType) -> tuple[type, type, str, dict[str, str]]:
    """Return the artifact model, join model, join ID field, and join defaults."""
    if artifact_type == "skill":
        return Skill, BundleSkill, "skill_id", {"source": "custom-added"}
    if artifact_type == "personality":
        return Personality, BundlePersonality, "personality_id", {}
    return CompositeLoop, BundleCompositeLoop, "composite_loop_id", {}


def set_liked_artifact(
    db: Session,
    *,
    owner_id: UUID,
    artifact_type: ArtifactType,
    artifact_id: UUID,
    liked: bool,
    ctx: AuthContext | None = None,
) -> dict[str, str | bool]:
    """Idempotently add or remove one artifact from the owner's Liked bundle.

    When ``ctx`` is supplied and the artifact is a skill, the caller must be
    able to READ that skill (authz.can_read_skill) — you cannot like a private
    skill you are not entitled to see. Personalities and loops are catalog-
    public artifacts, so no per-artifact read gate applies to them today.
    """
    bundle = ensure_liked_bundle(db, owner_id)
    artifact_model, join_model, artifact_id_field, join_defaults = _artifact_spec(artifact_type)
    artifact = db.query(artifact_model).filter(artifact_model.id == artifact_id).first()
    if artifact is None:
        raise LikedArtifactNotFoundError

    # authz gate (secfix_1905-B: every mutation surface carries an authz.can_* call).
    # Liking a skill requires read-access to it; unliking never needs a fresh read
    # (you are only removing a reference from your own bundle).
    if ctx is not None and liked and artifact_type == "skill":
        if not authz.can_read_skill(ctx, artifact, db=db):
            raise LikedArtifactForbiddenError

    existing = (
        db.query(join_model)
        .filter(join_model.bundle_id == bundle.id, getattr(join_model, artifact_id_field) == artifact_id)
        .first()
    )
    changed = False
    if liked and existing is None:
        db.add(
            join_model(
                bundle_id=bundle.id,
                **{artifact_id_field: artifact_id},
                **join_defaults,
            )
        )
        changed = True
    elif not liked and existing is not None:
        db.delete(existing)
        changed = True

    if changed:
        _touch_bundle_generation(db, bundle.id)
        db.commit()

    return {"liked": liked, "type": artifact_type, "id": str(artifact_id)}


def liked_library(db: Session, *, owner_id: UUID) -> dict:
    """Return the owner's typed liked shelves and the reserved follows shelf."""
    bundle = ensure_liked_bundle(db, owner_id)
    return {
        "liked_bundle_id": str(bundle.id),
        "shelves": {
            "skills": _liked_skills_shelf(db, owner_id=owner_id, bundle_id=bundle.id),
            "personalities": _liked_shelf(db, BundlePersonality, Personality, "personality_id", bundle.id),
            "loops": _liked_shelf(db, BundleCompositeLoop, CompositeLoop, "composite_loop_id", bundle.id),
        },
        "followed_bundles": _followed_bundles(db, owner_id),
    }


def _liked_skills_shelf(db: Session, *, owner_id: UUID, bundle_id: UUID) -> list[dict]:
    """Serialize the skills shelf as local Liked-bundle rows UNION federated likes.

    ponytail_0724. LoopSkill stores skill likes in two places and only one of
    them can represent a skill we do not host:

    - ``BundleSkill`` (the Liked bundle join) — LOCAL catalog skills only. This
      join also drives ``authz.can_install`` and fleet reconcile, so we must
      NEVER write a federated row into it: that would hand out install rights
      for a skill we neither host nor vet.
    - ``SkillLike`` — carries ``federated_source`` + ``federated_slug`` for hub /
      skills.sh / ClawHub tracks (``skill_id`` NULL).

    Before this fix ``liked_library`` read only the first, so hearting a hub
    skill wrote a row that the library then dropped — a button that lights up
    and forgets. Adam's call (2026-07-24): unify on the READ path. No schema
    change, no authz surface change.

    Local rows recorded in BOTH systems are de-duplicated by ``skill_id`` so a
    skill liked through either surface appears exactly once. Every row carries
    ``source`` and ``federated`` so the UI can badge external entries.
    """
    rows: list[dict] = []

    local = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == bundle_id)
        .order_by(BundleSkill.added_at.asc())
        .all()
    )
    for join, skill in local:
        rows.append(
            {
                "id": str(skill.id),
                "slug": skill.slug,
                "title": skill.title,
                "liked_at": join.added_at,
                "source": "local",
                "federated": False,
            }
        )

    likes = (
        db.query(SkillLike)
        .filter(
            SkillLike.user_id == owner_id,
            SkillLike.skill_id.is_(None),
            SkillLike.federated_source.isnot(None),
            SkillLike.federated_slug.isnot(None),
        )
        .order_by(SkillLike.liked_at.asc())
        .all()
    )
    for like in likes:
        rows.append(
            {
                # Federated tracks have no local artifact UUID and must not
                # pretend otherwise — the UI keys them off (source, slug).
                "id": None,
                "slug": like.federated_slug,
                "title": like.federated_slug,
                "liked_at": like.liked_at,
                "source": like.federated_source,
                "federated": True,
            }
        )

    return rows


def _liked_shelf(
    db: Session,
    join_model: type,
    artifact_model: type,
    artifact_id_field: str,
    bundle_id: UUID,
) -> list[dict[str, str | datetime | None]]:
    """Serialize one ordered liked-artifact shelf without leaking join details."""
    rows = (
        db.query(join_model, artifact_model)
        .join(artifact_model, artifact_model.id == getattr(join_model, artifact_id_field))
        .filter(join_model.bundle_id == bundle_id)
        .order_by(join_model.added_at.asc())
        .all()
    )
    return [
        {
            "id": str(artifact.id),
            "slug": artifact.slug,
            "title": artifact.title,
            "liked_at": join.added_at,
        }
        for join, artifact in rows
    ]


def _followed_bundles(db: Session, owner_id: UUID) -> list[dict[str, str | datetime | None]]:
    """Serialize public bundles the caller follows without exposing their members."""
    from app.models import Bundle

    rows = (
        db.query(FollowedBundle, Bundle, User)
        .join(Bundle, Bundle.id == FollowedBundle.bundle_id)
        .outerjoin(User, User.id == Bundle.bundle_owner)
        .filter(FollowedBundle.user_id == owner_id)
        .order_by(FollowedBundle.followed_at.desc())
        .all()
    )
    return [
        {
            "id": str(bundle.id),
            "slug": bundle.slug,
            "name": bundle.name,
            "owner_handle": owner.display_name if owner is not None else None,
            "followed_at": followed.followed_at,
        }
        for followed, bundle, owner in rows
    ]
