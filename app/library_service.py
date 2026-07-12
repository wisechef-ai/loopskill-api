"""Liked-library reads and mutations shared by HTTP and MCP surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.bundle_routes import _touch_bundle_generation
from app.liked_service import ensure_liked_bundle
from app.models import (
    BundleCompositeLoop,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    Personality,
    Skill,
)

ArtifactType = Literal["skill", "personality", "loop"]


class LikedArtifactNotFoundError(Exception):
    """Raised when a requested liked-library artifact does not exist."""


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
) -> dict[str, str | bool]:
    """Idempotently add or remove one artifact from the owner's Liked bundle."""
    bundle = ensure_liked_bundle(db, owner_id)
    artifact_model, join_model, artifact_id_field, join_defaults = _artifact_spec(artifact_type)
    artifact = db.query(artifact_model).filter(artifact_model.id == artifact_id).first()
    if artifact is None:
        raise LikedArtifactNotFoundError

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
            "skills": _liked_shelf(db, BundleSkill, Skill, "skill_id", bundle.id),
            "personalities": _liked_shelf(db, BundlePersonality, Personality, "personality_id", bundle.id),
            "loops": _liked_shelf(db, BundleCompositeLoop, CompositeLoop, "composite_loop_id", bundle.id),
        },
        "followed_bundles": [],
    }


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
