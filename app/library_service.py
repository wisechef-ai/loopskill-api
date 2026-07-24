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


def set_local_like_by_skill(
    db: Session,
    *,
    owner_id: UUID,
    skill: Skill,
    liked: bool,
) -> None:
    """Mirror a slug-route like of a LOCAL skill into the deployable Liked bundle.

    ponytail_0724 R2 self-audit. The browse/home heart posts to the SLUG route
    (``POST /api/skills/{slug}/like``) — it must, because a federated skill has
    no local UUID and so cannot use the UUID-based ``POST /api/library/like``.

    For a LOCAL catalog skill that route previously wrote only engagement state
    (``SkillLike(skill_id=...)``), while ``liked_library`` reads the
    ``BundleSkill`` join. Result: hearting a local skill put it NOWHERE the user
    could see it — the same lying-button bug as the federated case, on the more
    common path.

    So a local like is ALSO reflected into the caller's Liked bundle, which is
    the artifact's real destination (and what makes it reconcile onto their
    agents). Idempotent in both directions. Federated likes deliberately do NOT
    come through here: we do not host them, and ``BundleSkill`` drives
    ``authz.can_install`` + fleet reconcile.

    Authorization note: the caller (the slug route) has already resolved the
    skill from a public catalog lookup. This helper does not widen access — it
    only adds a reference to the caller's OWN Liked bundle.
    """
    bundle = ensure_liked_bundle(db, owner_id)
    existing = (
        db.query(BundleSkill)
        .filter(BundleSkill.bundle_id == bundle.id, BundleSkill.skill_id == skill.id)
        .first()
    )
    if liked and existing is None:
        db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
        _touch_bundle_generation(db, bundle.id)
        db.flush()
    elif not liked and existing is not None:
        db.delete(existing)
        _touch_bundle_generation(db, bundle.id)
        db.flush()


def liked_library(db: Session, *, owner_id: UUID) -> dict:
    """Return the owner's typed liked shelves and the reserved follows shelf.

    ponytail_0724 R1 (Codex MUST-FIX #2 / #7 / #8). The typed ``shelves``
    payload is a FROZEN CONTRACT (``docs/briefs/liked_0711-P1.md`` §FROZEN
    CONTRACT): each entry is exactly ``{id, slug, title, liked_at}`` with a UUID
    ``id``, and the Liked bundle it serialises is a DEPLOYABLE bundle — the
    reconcile path pulls it onto the caller's agents.

    An earlier cut of this fix merged federated (hub / skills.sh / ClawHub)
    likes INTO ``shelves.skills`` with ``id: None``. That was wrong twice over:
    it broke the pinned shape for every existing consumer, and it implied
    agents could deploy artifacts we neither host nor vet.

    Federated likes are therefore served on their OWN additive key,
    ``federated_skills`` — new, so it breaks nothing, and structurally separate,
    so nothing can mistake a community bookmark for a deployable Liked entry.
    """
    bundle = ensure_liked_bundle(db, owner_id)
    return {
        "liked_bundle_id": str(bundle.id),
        "shelves": {
            "skills": _liked_shelf(db, BundleSkill, Skill, "skill_id", bundle.id),
            "personalities": _liked_shelf(db, BundlePersonality, Personality, "personality_id", bundle.id),
            "loops": _liked_shelf(db, BundleCompositeLoop, CompositeLoop, "composite_loop_id", bundle.id),
        },
        # Additive (ponytail_0724). NOT part of the deployable Liked bundle.
        "federated_skills": _federated_liked_skills(db, owner_id=owner_id),
        "followed_bundles": _followed_bundles(db, owner_id),
    }


def _federated_liked_skills(db: Session, *, owner_id: UUID) -> list[dict]:
    """Serialize the caller's FEDERATED skill likes (hub / skills.sh / ClawHub).

    ponytail_0724. These live in ``skill_likes`` with ``skill_id IS NULL`` and a
    ``(federated_source, federated_slug)`` identity — the only representation
    that can name a skill we do not host. They are deliberately NOT merged into
    ``shelves.skills``:

    - ``shelves.skills`` is the DEPLOYABLE Liked bundle (``BundleSkill``), which
      also drives ``authz.can_install`` and fleet reconcile. Putting a federated
      row there would grant install rights for unvetted third-party content.
    - its entry shape is a frozen contract (``{id, slug, title, liked_at}``,
      ``id`` a UUID). A federated row has no local UUID.

    Rows are newest-last (ascending ``liked_at``) to match the typed shelves'
    ordering convention, and carry ``source`` so the UI can badge them.
    """
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
    return [
        {
            "slug": like.federated_slug,
            "title": like.federated_slug,
            "source": like.federated_source,
            "liked_at": like.liked_at,
        }
        for like in likes
    ]


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
