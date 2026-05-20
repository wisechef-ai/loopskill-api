"""recipes_share_* — Phase D MCP tools for cookbook share-token management.

4 tools:
  recipes_share_create  — create a new share token (returns config_blocks)
  recipes_share_list    — list tokens for a cookbook (metadata only)
  recipes_share_revoke  — soft-delete (deactivate) a token
  recipes_share_rotate  — deactivate old + create new (returns config_blocks)

All 4 require can_write_cookbook(ctx, cookbook).
Service logic is delegated to app.share_token_routes._*_service helpers so
REST routes and MCP tools stay in sync with zero code duplication.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app._config_block_formatter import build_config_blocks
from app.models import Cookbook
from app.share_token_routes import (
    _create_service,
    _list_service,
    _revoke_service,
    _rotate_service,
)


def _load_cookbook(db: Session, cookbook_id: str) -> Cookbook | None:
    """Load a Cookbook by UUID string; return None on bad ID or missing row."""
    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        return None
    return db.query(Cookbook).filter(Cookbook.id == cid).first()


def recipes_share_create(
    db: Session,
    *,
    cookbook_id: str,
    name: str | None = None,
    scope: str = "edit",
    ctx: AuthContext | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Create a new share token for a cookbook.

    Returns token (plaintext, shown once), prefix, scope, name, id,
    created_at, and config_blocks (hermes_yaml + claude_desktop_json).

    Authz: requires can_write_cookbook(ctx, cookbook).
    """
    if ctx is None:
        ctx = AuthContext(scope="master")

    cb = _load_cookbook(db, cookbook_id)
    if cb is None:
        return {"error": "cookbook_not_found", "cookbook_id": cookbook_id}

    if not authz.can_write_cookbook(ctx, cb):
        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}

    # Delegate to the service (raises HTTPException on invalid scope)
    try:
        result = _create_service(
            db,
            cookbook=cb,
            name=name,
            scope=scope,
            created_by=ctx.user_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Rationale: HTTPException from _create_service (invalid_scope) must be
        # surfaced as an error dict rather than crashing the MCP transport.
        detail = getattr(exc, "detail", str(exc))
        return {"error": str(detail), "cookbook_id": cookbook_id}

    # Add config_blocks for create
    result["config_blocks"] = build_config_blocks(
        token=result["token"],
        cookbook_id=cookbook_id,
    )
    return result


def recipes_share_list(
    db: Session,
    *,
    cookbook_id: str,
    ctx: AuthContext | None = None,
    **_: Any,
) -> dict[str, Any]:
    """List share tokens for a cookbook (metadata only, no plaintext).

    Returns {"tokens": [{id, prefix, name, scope, is_active, created_at, last_used_at}]}.

    Authz: requires can_write_cookbook(ctx, cookbook).
    """
    if ctx is None:
        ctx = AuthContext(scope="master")

    cb = _load_cookbook(db, cookbook_id)
    if cb is None:
        return {"error": "cookbook_not_found", "cookbook_id": cookbook_id}

    if not authz.can_write_cookbook(ctx, cb):
        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}

    tokens = _list_service(db, cookbook=cb)
    return {"tokens": tokens}


def recipes_share_revoke(
    db: Session,
    *,
    cookbook_id: str,
    token_id: str,
    ctx: AuthContext | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Soft-delete (deactivate) a share token immediately.

    Returns {"revoked": true, "token_id": "<id>"}.

    Authz: requires can_write_cookbook(ctx, cookbook).
    """
    if ctx is None:
        ctx = AuthContext(scope="master")

    cb = _load_cookbook(db, cookbook_id)
    if cb is None:
        return {"error": "cookbook_not_found", "cookbook_id": cookbook_id}

    if not authz.can_write_cookbook(ctx, cb):
        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}

    try:
        _revoke_service(db, cookbook=cb, token_id=token_id)
    except Exception as exc:  # noqa: BLE001
        # Rationale: HTTPException 404 from _revoke_service (token not found)
        # must surface as an error dict rather than crashing the MCP transport.
        detail = getattr(exc, "detail", str(exc))
        return {"error": str(detail), "token_id": token_id}

    return {"revoked": True, "token_id": token_id}


def recipes_share_rotate(
    db: Session,
    *,
    cookbook_id: str,
    token_id: str,
    ctx: AuthContext | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Rotate a share token: deactivate old, create new (same name/scope).

    Returns new_token, new_prefix, old_token_id, new_token_id, and
    config_blocks (hermes_yaml + claude_desktop_json) for the new token.

    Authz: requires can_write_cookbook(ctx, cookbook).
    """
    if ctx is None:
        ctx = AuthContext(scope="master")

    cb = _load_cookbook(db, cookbook_id)
    if cb is None:
        return {"error": "cookbook_not_found", "cookbook_id": cookbook_id}

    if not authz.can_write_cookbook(ctx, cb):
        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}

    try:
        result = _rotate_service(
            db,
            cookbook=cb,
            token_id=token_id,
            created_by=ctx.user_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Rationale: HTTPException 404 from _rotate_service (token not found)
        # must surface as an error dict rather than crashing the MCP transport.
        detail = getattr(exc, "detail", str(exc))
        return {"error": str(detail), "token_id": token_id}

    config_blocks = build_config_blocks(
        token=result["token"],
        cookbook_id=cookbook_id,
    )

    return {
        "new_token": result["token"],
        "new_prefix": result["prefix"],
        "old_token_id": result["old_token_id"],
        "new_token_id": result["id"],
        "config_blocks": config_blocks,
    }
