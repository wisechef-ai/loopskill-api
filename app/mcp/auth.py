"""MCP key validation — operator master key + Phase C sub-recipe stubs.

Returned scopes:
    * ``operator``    — the master ``settings.API_KEY`` or a real APIKey row.
    * ``sub_recipe``  — Phase C; not wired here, raises NotImplementedError.
    * ``unauthorized`` — anything else.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import APIKey


def validate_key(key: str | None, db: Session) -> dict[str, Any]:
    """Validate an MCP caller key.

    Mirrors the existing middleware logic in ``app/middleware.py`` so the SSE
    transport accepts the same keys as the REST API. Sub-recipe keys are a
    Phase C concern — calling validate_key with a sub_ prefix raises
    NotImplementedError per spec.
    """
    if not key:
        return {"scope": "unauthorized", "user_id": None, "api_key_id": None}

    if key.startswith("sub_"):
        raise NotImplementedError("phase-C")

    if not key.startswith("rec_"):
        return {"scope": "unauthorized", "user_id": None, "api_key_id": None}

    if key == settings.API_KEY:
        # Master operator key — no per-user identity, no APIKey row.
        return {"scope": "operator", "user_id": None, "api_key_id": None}

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    api_key_obj = (
        db.query(APIKey)
        .filter(APIKey.key_hash == key_hash, APIKey.is_active == True)  # noqa: E712
        .first()
    )
    if api_key_obj:
        return {
            "scope": "operator",
            "user_id": api_key_obj.user_id,
            "api_key_id": api_key_obj.id,
        }

    return {"scope": "unauthorized", "user_id": None, "api_key_id": None}
