"""MCP verb for mutating a caller's typed Liked library."""

from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.library_service import LikedArtifactNotFoundError, set_liked_artifact


def recipes_like(
    db: Session,
    *,
    action: Literal["like", "unlike"],
    type: Literal["skill", "personality", "loop"],
    id: str,
    ctx: AuthContext,
) -> dict:
    """Idempotently like or unlike one artifact in the authenticated user's library."""
    if ctx.user_id is None:
        return {"error": "forbidden", "detail": "Authentication required"}
    try:
        artifact_id = UUID(id)
    except (ValueError, AttributeError):
        return {"error": "invalid_id", "id": id}
    if action not in {"like", "unlike"}:
        return {"error": "invalid_action", "action": action}
    if type not in {"skill", "personality", "loop"}:
        return {"error": "invalid_type", "type": type}
    try:
        return set_liked_artifact(
            db,
            owner_id=ctx.user_id,
            artifact_type=type,
            artifact_id=artifact_id,
            liked=action == "like",
        )
    except LikedArtifactNotFoundError:
        return {"error": "not_found", "type": type, "id": id}
