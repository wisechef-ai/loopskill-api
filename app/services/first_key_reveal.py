"""Ephemeral one-time reveal store for the auto-minted first API key.

flywheel F1.2: the OAuth callback mints a `rec_` key server-side and cannot
hand the plaintext back over a redirect response (a 302 has no body worth
trusting, and the plaintext must NEVER ride a URL — query params land in
logs/referrers/browser history). The ``api_keys`` table itself never stores
plaintext, only the sha256 hash; this module holds the SAME posture — the
plaintext is held in a short-TTL, single-read store keyed by an opaque
reveal token that rides a short-lived HttpOnly cookie to the landing page.
The portal's post-signup card fetches it ONCE over the normal authed session
(``credentials: 'include'``); the entry (and the cookie) are then destroyed.

Falls back to an in-memory dict when Redis is unavailable — same
degrade-gracefully posture as ``app.last_used_tracker.LastUsedTracker``.
The in-memory fallback does not survive a process restart or work across
multiple workers; a reveal that misses simply degrades to "nothing to
show" (404) on the portal side — the plaintext is never recoverable after
mint either way, mirroring the existing ``POST /api/api-keys`` contract
("Save this key now — it will not be shown again.").
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

REVEAL_COOKIE_NAME = "wr_first_key_reveal"
REVEAL_TTL_SECONDS = 600  # 10 minutes — plenty for a redirect + one page load
_REDIS_KEY_PREFIX = "wr:first_key_reveal:"

# In-memory fallback: {token: {"user_id": str, "key": str, "prefix": str, "label": str}}
_memory_store: dict[str, dict[str, Any]] = {}


def store_reveal(user_id: UUID, plaintext: str, prefix: str, label: str) -> str:
    """Persist the plaintext ephemerally and return an opaque reveal token."""
    token = secrets.token_urlsafe(24)
    payload = {"user_id": str(user_id), "key": plaintext, "prefix": prefix, "label": label}

    from app.middleware import get_redis

    redis_client = get_redis()
    if redis_client is not None:
        try:
            redis_client.setex(f"{_REDIS_KEY_PREFIX}{token}", REVEAL_TTL_SECONDS, json.dumps(payload))
            return token
        # Rationale: reveal-store write is best-effort; Redis failure degrades to memory.
        except Exception as exc:  # noqa: BLE001
            logger.warning("first_key_reveal: Redis write failed, falling back to memory: %s", exc)

    _memory_store[token] = payload
    return token


def consume_reveal(token: str, user_id: UUID) -> dict[str, Any] | None:
    """Return + delete the stored payload for ``token`` iff it belongs to ``user_id``.

    One-time read: the entry is deleted on both the hit and (when found but
    owned by someone else) the mismatch path, so a stolen/guessed token
    cannot be replayed even once it has been looked up.
    """
    from app.middleware import get_redis

    redis_client = get_redis()
    payload: dict[str, Any] | None = None
    if redis_client is not None:
        try:
            raw = redis_client.get(f"{_REDIS_KEY_PREFIX}{token}")
            if raw is not None:
                payload = json.loads(raw)
                redis_client.delete(f"{_REDIS_KEY_PREFIX}{token}")
        # Rationale: reveal-store read is best-effort; Redis failure degrades to memory.
        except Exception as exc:  # noqa: BLE001
            logger.warning("first_key_reveal: Redis read failed, falling back to memory: %s", exc)

    if payload is None:
        payload = _memory_store.pop(token, None)

    if payload is None:
        return None
    if payload.get("user_id") != str(user_id):
        return None
    return payload
