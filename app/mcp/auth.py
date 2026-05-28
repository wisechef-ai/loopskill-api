"""MCP key validation — proper scope resolution via AuthContext.

Returned scopes mirror app/auth_ctx.py exactly (single source of truth):
    * ``master``      — the master ``settings.API_KEY`` (hmac.compare_digest).
    * ``user``        — a real APIKey row hit (NOT the old legacy 'operator' scope).
    * ``anonymous``   — no key provided.
    * ``cbt_token``   — cookbook share token (see middleware.py cbt_ path).
    * ``unauthorized``— key provided but not recognised.

Phase B fix for Issue #5: every user key previously got scope='operator' (legacy alias —
(a superuser privilege — legacy scope value, pre-Phase-5); now correctly gets scope='user'.
The request.state.auth_ctx is populated with an AuthContext dataclass
identical to the REST path — single source of truth.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.config import settings
from app.models import APIKey


def validate_key(key: str | None, db: Session) -> dict[str, Any]:
    """Validate an MCP caller key and return a plain dict + populate AuthContext.

    Returns a dict with keys: scope, user_id, api_key_id, auth_ctx.
    The auth_ctx value is an AuthContext dataclass (same schema as REST).

    Mirrors the scope resolution in ``app/middleware.py`` so the SSE
    transport accepts the same keys as the REST API with identical semantics.
    """
    if not key:
        ctx = AuthContext.anonymous()
        return {
            "scope": "anonymous",
            "user_id": None,
            "api_key_id": None,
            "auth_ctx": ctx,
        }

    if key.startswith("sub_"):
        raise NotImplementedError("phase-C")

    if not key.startswith("rec_"):
        ctx = AuthContext.anonymous()
        return {
            "scope": "unauthorized",
            "user_id": None,
            "api_key_id": None,
            "auth_ctx": ctx,
        }

    # Master key check via timing-safe comparison (Issue #3, Phase A)
    if hmac.compare_digest(key, settings.API_KEY):
        ctx = AuthContext(scope="master")
        return {
            "scope": "master",
            "user_id": None,
            "api_key_id": None,
            "auth_ctx": ctx,
        }

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    api_key_obj = (
        db.query(APIKey)
        .filter(APIKey.key_hash == key_hash, APIKey.is_active == True)  # noqa: E712
        .first()
    )
    if api_key_obj:
        ctx = AuthContext(
            scope="user",
            user_id=api_key_obj.user_id,
            api_key_id=api_key_obj.id,
            cookbook_scope=api_key_obj.cookbook_id,  # None if not scoped
        )
        return {
            "scope": "user",
            "user_id": api_key_obj.user_id,
            "api_key_id": api_key_obj.id,
            "auth_ctx": ctx,
        }

    return {
        "scope": "unauthorized",
        "user_id": None,
        "api_key_id": None,
        "auth_ctx": AuthContext.anonymous(),
    }
