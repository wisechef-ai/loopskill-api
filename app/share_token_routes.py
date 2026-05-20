"""Cookbook share-token endpoints — v7.1 Phase 3.

Routes (mounted under /api/cookbooks/{cookbook_id}/share-tokens):
  POST   ""            — create token (plaintext returned once)
  GET    ""            — list tokens (metadata only, no plaintext)
  POST   /{token_id}/rotate — deactivate old, create new
  DELETE /{token_id}   — soft-delete (is_active=False)

Auth: rec_-key user must own the cookbook (or master key).
Scope enforcement via enforce_cbt_scope() helper.

Phase D (recipes_2005): Service functions extracted so MCP tools can call the
same logic. Routes are unchanged in behaviour — they delegate to _*_service
helpers and return the same responses as before.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Cookbook, CookbookShareToken

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/cookbooks/{cookbook_id}/share-tokens",
    tags=["share-tokens"],
)


# ── Schemas ──────────────────────────────────────────────────────────────


class ShareTokenCreateIn(BaseModel):
    name: str | None = None
    scope: str | None = "edit"


class ShareTokenOut(BaseModel):
    id: str
    token: str | None = None  # Only populated on create/rotate
    prefix: str
    scope: str
    name: str | None = None
    created_at: datetime | None = None


class ShareTokenListItem(BaseModel):
    id: str
    prefix: str
    name: str | None = None
    scope: str
    created_at: datetime | None = None
    is_active: bool
    last_used_at: datetime | None = None


# ── Scope enforcement helper ────────────────────────────────────────────


def enforce_cbt_scope(request: Request) -> None:
    """Raise 403 if a cbt_ token is being used outside its scope.

    This function should be called from cookbook routes (and share-token
    routes themselves) when a cbt_ token may be present.

    Rules:
      - If no cbt_ token state → no-op (rec_ key path unaffected).
      - If cbt_ token's cookbook_id != route's cookbook_id → 403 wrong cookbook.
      - If scope == 'read' and method != GET → 403 read-only.
      - _publish path is always 403 for cbt_ tokens.
    """
    scope = getattr(request.state, "cookbook_token_scope", None)
    if scope is None:
        return  # No cbt_ token; rec_ key path — pass through

    # _publish is always blocked for cbt_ tokens
    if request.url.path.endswith("/_publish") or "/_publish" in request.url.path:
        raise HTTPException(
            status_code=403,
            detail="Share tokens cannot authorize publishing",
        )

    # Read-only scope can only do GET
    if scope == "read" and request.method != "GET":
        raise HTTPException(
            status_code=403,
            detail="Token scope mismatch (read-only)",
        )


def _get_cookbook_and_check_scope(
    request: Request,
    db: Session,
    cookbook_id: str,
) -> Cookbook:
    """Load cookbook and enforce cbt_ scope rules.

    Returns the Cookbook if all checks pass.
    """
    scope = getattr(request.state, "cookbook_token_scope", None)
    token_cookbook_id = getattr(request.state, "cookbook_token_cookbook_id", None)

    if scope is not None:
        # cbt_ token present — check cookbook match
        try:
            cid = UUID(cookbook_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=404, detail="cookbook_not_found")

        if token_cookbook_id != cid:
            raise HTTPException(
                status_code=403,
                detail="Token scope mismatch (wrong cookbook)",
            )

    # Load the cookbook
    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    cb = db.query(Cookbook).filter(Cookbook.id == cid).first()
    if cb is None:
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    # Enforce scope rules (read-only, _publish)
    enforce_cbt_scope(request)

    return cb


# ── Auth helpers ─────────────────────────────────────────────────────────


def _require_owner(request: Request, db: Session, cookbook_id: str) -> Cookbook:
    """Require that the caller owns the cookbook (rec_ key user) or is master.

    cbt_ tokens CANNOT create/manage share tokens (only rec_ keys can).
    """
    # If a cbt_ token is present, block management operations
    scope = getattr(request.state, "cookbook_token_scope", None)
    if scope is not None:
        raise HTTPException(
            status_code=403,
            detail="Share tokens cannot manage share tokens",
        )

    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    is_master = api_key_user_id is None  # master key has None

    if api_key_user_id == "MISSING":
        raise HTTPException(status_code=401, detail="auth_required")

    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    cb = db.query(Cookbook).filter(Cookbook.id == cid).first()
    if cb is None:
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    if not is_master and cb.cookbook_owner != api_key_user_id:
        raise HTTPException(status_code=403, detail="not_cookbook_owner")

    return cb


def _generate_token(cookbook_id: UUID) -> tuple[str, str, str]:
    """Generate a share token for a cookbook.

    Returns (plaintext_token, token_hash, token_prefix).
    """
    cb_prefix = str(cookbook_id).replace("-", "")[:8]
    random_hex = secrets.token_hex(16)
    full_token = f"cbt_{cb_prefix}_{random_hex}"
    token_hash = hashlib.sha256(full_token.encode()).hexdigest()
    return full_token, token_hash, cb_prefix


# ── Service functions (Phase D extraction) ──────────────────────────────
# Each _*_service function contains the core business logic previously
# inlined in the route handler. Routes now delegate to these helpers.
# MCP tools (app/mcp/tools/share.py) also call these helpers directly.


def _create_service(
    db: Session,
    *,
    cookbook: Cookbook,
    name: str | None = None,
    scope: str = "edit",
    created_by=None,
) -> dict:
    """Core logic for creating a share token.

    Args:
        db: Database session.
        cookbook: The Cookbook ORM object (already ownership-checked).
        name: Optional human-readable label.
        scope: 'read' or 'edit' (default 'edit').
        created_by: User ID of the creator (None for master key).

    Returns:
        dict with id, token, prefix, scope, name, created_at.
    """
    if scope not in ("read", "edit"):
        raise HTTPException(status_code=422, detail="invalid_scope")

    full_token, token_hash, token_prefix = _generate_token(cookbook.id)

    row = CookbookShareToken(
        id=uuid4(),
        cookbook_id=cookbook.id,
        token_hash=token_hash,
        token_prefix=token_prefix,
        scope=scope,
        name=name,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "id": str(row.id),
        "token": full_token,
        "prefix": token_prefix,
        "scope": row.scope,
        "name": row.name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _list_service(db: Session, *, cookbook: Cookbook) -> list[dict]:
    """Core logic for listing share tokens for a cookbook.

    Args:
        db: Database session.
        cookbook: The Cookbook ORM object (already ownership-checked).

    Returns:
        List of token metadata dicts (no plaintext).
    """
    rows = (
        db.query(CookbookShareToken)
        .filter(CookbookShareToken.cookbook_id == cookbook.id)
        .order_by(CookbookShareToken.created_at.desc())
        .all()
    )

    return [
        {
            "id": str(r.id),
            "prefix": r.token_prefix,
            "name": r.name,
            "scope": r.scope,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "is_active": r.is_active,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        }
        for r in rows
    ]


def _rotate_service(
    db: Session,
    *,
    cookbook: Cookbook,
    token_id: str,
    created_by=None,
) -> dict:
    """Core logic for rotating a share token.

    Deactivates the old token and creates a new one with the same name/scope.

    Args:
        db: Database session.
        cookbook: The Cookbook ORM object (already ownership-checked).
        token_id: UUID string of the token to rotate.
        created_by: User ID for the new token row.

    Returns:
        dict with id, token, prefix, scope, name, created_at (the new token).
    """
    try:
        tid = UUID(token_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="token_not_found")

    old = (
        db.query(CookbookShareToken)
        .filter(
            CookbookShareToken.id == tid,
            CookbookShareToken.cookbook_id == cookbook.id,
        )
        .with_for_update()  # SECURITY: serialize concurrent rotates so two
        # parallel calls cannot both produce a new active
        # row. The lock is dropped on commit/rollback.
        .first()
    )
    if old is None or not old.is_active:
        raise HTTPException(status_code=404, detail="token_not_found")

    # Deactivate old
    old.is_active = False

    # Create new with same name + scope
    full_token, token_hash, token_prefix = _generate_token(cookbook.id)

    new_row = CookbookShareToken(
        id=uuid4(),
        cookbook_id=cookbook.id,
        token_hash=token_hash,
        token_prefix=token_prefix,
        scope=old.scope,
        name=old.name,
        created_by=created_by,
    )
    db.add(new_row)
    db.commit()
    db.refresh(new_row)

    return {
        "id": str(new_row.id),
        "token": full_token,
        "prefix": token_prefix,
        "scope": new_row.scope,
        "name": new_row.name,
        "created_at": new_row.created_at.isoformat() if new_row.created_at else None,
        "old_token_id": str(old.id),
    }


def _revoke_service(
    db: Session,
    *,
    cookbook: Cookbook,
    token_id: str,
) -> None:
    """Core logic for revoking (soft-deleting) a share token.

    Args:
        db: Database session.
        cookbook: The Cookbook ORM object (already ownership-checked).
        token_id: UUID string of the token to revoke.

    Raises:
        HTTPException 404 if token not found for this cookbook.
    """
    try:
        tid = UUID(token_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="token_not_found")

    row = (
        db.query(CookbookShareToken)
        .filter(
            CookbookShareToken.id == tid,
            CookbookShareToken.cookbook_id == cookbook.id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="token_not_found")

    row.is_active = False
    db.commit()


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_share_token(
    cookbook_id: str,
    body: ShareTokenCreateIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a new share token. Plaintext token returned exactly once."""
    cb = _require_owner(request, db, cookbook_id)
    created_by = getattr(request.state, "api_key_user_id", None)
    return _create_service(
        db,
        cookbook=cb,
        name=body.name,
        scope=body.scope or "edit",
        created_by=created_by,
    )


@router.get("")
def list_share_tokens(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """List all share tokens for a cookbook (metadata only, no plaintext)."""
    cb = _require_owner(request, db, cookbook_id)
    return _list_service(db, cookbook=cb)


@router.post("/{token_id}/rotate")
def rotate_share_token(
    cookbook_id: str,
    token_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Deactivate old token and create a new one with the same name/scope."""
    cb = _require_owner(request, db, cookbook_id)
    created_by = getattr(request.state, "api_key_user_id", None)
    result = _rotate_service(db, cookbook=cb, token_id=token_id, created_by=created_by)
    # Route returns the same shape as before (id/token/prefix/scope/name/created_at)
    return {
        "id": result["id"],
        "token": result["token"],
        "prefix": result["prefix"],
        "scope": result["scope"],
        "name": result["name"],
        "created_at": result["created_at"],
    }


@router.delete("/{token_id}", status_code=204)
def revoke_share_token(
    cookbook_id: str,
    token_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Soft-delete a share token (sets is_active=False)."""
    cb = _require_owner(request, db, cookbook_id)
    _revoke_service(db, cookbook=cb, token_id=token_id)
    return None
