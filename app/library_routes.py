"""HTTP API for a caller's typed Liked library."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.library_service import LikedArtifactNotFoundError, liked_library, set_liked_artifact

router = APIRouter(prefix="/api/library", tags=["library"])


class LikeRequest(BaseModel):
    """An artifact to add to or remove from the caller's Liked bundle."""

    type: Literal["skill", "personality", "loop"]
    id: UUID


def _authenticated_owner(request: Request) -> UUID:
    """Require an authenticated user because Liked bundles are per-user."""
    ctx = getattr(request.state, "auth_ctx", None)
    if not isinstance(ctx, AuthContext) or ctx.user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return ctx.user_id


@router.get("")
def get_library(request: Request, db: Session = Depends(get_db)) -> dict:
    """Return the caller's Liked bundle as typed shelves."""
    return liked_library(db, owner_id=_authenticated_owner(request))


@router.post("/like")
def like_artifact(payload: LikeRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Idempotently add one artifact to the caller's Liked bundle."""
    try:
        return set_liked_artifact(
            db,
            owner_id=_authenticated_owner(request),
            artifact_type=payload.type,
            artifact_id=payload.id,
            liked=True,
        )
    except LikedArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc


@router.delete("/like")
def unlike_artifact(payload: LikeRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    """Idempotently remove one artifact from the caller's Liked bundle."""
    try:
        return set_liked_artifact(
            db,
            owner_id=_authenticated_owner(request),
            artifact_type=payload.type,
            artifact_id=payload.id,
            liked=False,
        )
    except LikedArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
