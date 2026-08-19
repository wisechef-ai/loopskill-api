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

    # qa0208-w3 dual-accept: lsk_ (canonical) accepted alongside legacy rec_.
    if not (key.startswith("rec_") or key.startswith("lsk_")):
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
        # agentreg_0819: identity-revocation gate for self-registered agents.
        # MCP is the universal path — a REST-only revocation is not a
        # revocation, so the SAME helper the middleware uses runs here. A
        # revoked/unknown identity resolves to 'unauthorized', identical to a
        # key that does not exist at all (no oracle for the revoked state).
        from app.middleware._agent_identity import (
            agent_key_is_blocked,
            is_agent_key,
            user_is_agent,
        )

        _is_agent_credential = is_agent_key(key)
        if _is_agent_credential and agent_key_is_blocked(db, api_key_obj.user_id):
            return {
                "scope": "unauthorized",
                "user_id": None,
                "api_key_id": None,
                "auth_ctx": AuthContext.anonymous(),
            }
        # agentreg_0819 (round 2, F5): stamp the durable agent marker so MCP
        # callers carry the same principal distinction REST does — "MCP is the
        # universal path; a REST-only fix is not a fix".
        #
        # Gated on the key prefix so no human MCP call pays for an extra query.
        # That is sufficient, not a shortcut: registration is the only path that
        # mints a key for an is_agent user, and it always uses the rec_agent_
        # prefix — the same premise the revocation gate directly above already
        # relies on. If a second agent credential type is ever added it must be
        # added to is_agent_key(), which is why that predicate is shared.
        _is_agent = bool(_is_agent_credential and user_is_agent(db, api_key_obj.user_id))
        # Resolve the caller's tenant the SAME way the REST path does
        # (app.middleware.api_key) — otherwise org-scoped features silently fail
        # closed over MCP (Lane C #4).
        #
        # mesh_0408 W1 (P0), codex review of PR #202 finding 2. This used to call
        # _resolve_org_membership, which resolves the OLDEST OrgMembership of the
        # key's user. Every fleet-member key is minted with
        # user_id = fleet.owner_user_id, so over MCP every member key of an
        # account collapsed onto whichever org that account created FIRST — i.e.
        # the layer-2 fix was reproduced only for REST and discarded on every MCP
        # call, leaving the whole P0 exploitable over the universal path (lock
        # #31: MCP is the universal path; a REST-only fix is not a fix).
        # _resolve_org_for_key resolves a member key through its fleet and falls
        # back to membership only for genuine human keys.
        from app.middleware.api_key import _resolve_org_for_key

        org_id, is_org_owner = _resolve_org_for_key(db, api_key_obj.id, api_key_obj.user_id)
        ctx = AuthContext(
            scope="user",
            user_id=api_key_obj.user_id,
            api_key_id=api_key_obj.id,
            bundle_scope=api_key_obj.bundle_id,  # None if not scoped  # compat-alias
            org_id=org_id,
            is_org_owner=is_org_owner,
            is_agent=_is_agent,
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
