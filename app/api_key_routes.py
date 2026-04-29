"""API key management routes — generate, list, revoke.

WIS-640: Plan v4 — users need a `rec_*` key to use the meta-skill or any /api/* route.
This module provides the user-facing CRUD for it.

Design:
- One active key per user (regenerate revokes old + creates new in single txn).
- Plaintext key returned ONCE on creation. Never recoverable after.
- Stored as sha256 hash with 12-char prefix for lookup.
- Format: `rec_live_<32 random urlsafe chars>` — matches existing API_KEY_PREFIX in middleware.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.database import get_db
from app.models import APIKey, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api-keys"])


KEY_PREFIX = "rec_live_"
KEY_BODY_LEN = 32  # urlsafe chars after prefix


def _generate_key() -> tuple[str, str, str]:
    """Generate a fresh API key. Returns (plaintext, prefix12, sha256_hash)."""
    body = secrets.token_urlsafe(KEY_BODY_LEN)
    plaintext = f"{KEY_PREFIX}{body}"
    prefix12 = plaintext[:12]
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, prefix12, key_hash


def _require_user(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    return user


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Create a new API key for the authenticated user.

    Enforces "one active key per user": any existing active key is revoked
    in the same transaction. The plaintext key is returned ONCE — store it.
    """
    user = _require_user(user)

    # Revoke any existing active keys (one-per-user policy)
    existing = db.query(APIKey).filter(
        APIKey.user_id == user.id, APIKey.is_active == True,  # noqa: E712
    ).all()
    for k in existing:
        k.is_active = False

    plaintext, prefix12, key_hash = _generate_key()
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    name = (body or {}).get("name") if isinstance(body, dict) else None

    new_key = APIKey(
        user_id=user.id,
        key_prefix=prefix12,
        key_hash=key_hash,
        name=name or "default",
        is_active=True,
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)

    logger.info("Created API key %s for user %s (revoked %d previous)",
                new_key.id, user.id, len(existing))

    # Plaintext returned ONCE — never again
    return {
        "id": str(new_key.id),
        "key": plaintext,
        "prefix": prefix12,
        "name": new_key.name,
        "created_at": new_key.created_at.isoformat() if new_key.created_at else None,
        "warning": "Save this key now — it will not be shown again.",
    }


@router.get("/api-keys")
async def list_api_keys(
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """List the authenticated user's API keys (no plaintext)."""
    user = _require_user(user)
    keys = (
        db.query(APIKey)
        .filter(APIKey.user_id == user.id)
        .order_by(APIKey.created_at.desc())
        .all()
    )
    return {
        "keys": [
            {
                "id": str(k.id),
                "prefix": k.key_prefix,
                "name": k.name,
                "is_active": k.is_active,
                "created_at": k.created_at.isoformat() if k.created_at else None,
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            }
            for k in keys
        ],
    }


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Revoke an API key. Idempotent — already-revoked or missing keys return 204."""
    user = _require_user(user)
    try:
        key_uuid = UUID(key_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_key_id")

    key = (
        db.query(APIKey)
        .filter(APIKey.id == key_uuid, APIKey.user_id == user.id)
        .first()
    )
    if key and key.is_active:
        key.is_active = False
        db.commit()
        logger.info("Revoked API key %s for user %s", key.id, user.id)

    return {"revoked": True, "id": key_id}
