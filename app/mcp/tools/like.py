"""MCP verb for mutating a caller's typed Liked library."""

from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.library_service import (
    LikedArtifactForbiddenError,
    LikedArtifactNotFoundError,
    set_liked_artifact,
)
from app.mcp.tools.composite_loop_catalog import _NOT_HANDLED


def recipes_like(
    db: Session,
    *,
    action: Literal["like", "unlike"],
    type: Literal["skill", "personality", "loop"],
    id: str,
    ctx: AuthContext,
) -> dict:
    """Idempotently like or unlike one artifact in the authenticated user's library.

    Authorization: enforced at the shared chokepoint ``set_liked_artifact`` (so
    the HTTP route and this MCP verb share one gate). Liking a skill calls
    ``authz.can_read_skill`` there — you cannot like a private skill you may not
    read; the resulting ``LikedArtifactForbiddenError`` maps to a forbidden
    response below. Unlike only removes a reference from the caller's own bundle
    and needs no fresh read gate.
    """
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
            ctx=ctx,
        )
    except LikedArtifactForbiddenError:
        return {"error": "forbidden", "type": type, "id": id}
    except LikedArtifactNotFoundError:
        return {"error": "not_found", "type": type, "id": id}


# Uses the shared _NOT_HANDLED sentinel (imported from composite_loop_catalog)
# so the server's _dispatch chain can distinguish "handled" from "pass through".


def dispatch_library(name: str, db: Session, args: dict, ctx: AuthContext) -> object:
    """Dispatch the Liked-library MCP verbs; return _NOT_HANDLED for other names.

    Extracted from app/mcp/server.py to keep that module under the 600-line
    god-object cap (test_w0_2_pyfile_size_discipline). Same delegation pattern
    as dispatch_composite_loop.
    """
    if name == "recipes_like":
        return recipes_like(
            db,
            action=args.get("action"),
            type=args.get("type"),
            id=args.get("id"),
            ctx=ctx,
        )
    return _NOT_HANDLED
