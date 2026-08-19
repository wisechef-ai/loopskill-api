"""agentreg_0819 — agent self-registration + agent-identity revocation.

    POST   /api/agents/register                            (public, no auth)
    POST   /api/admin/agent-identities/{identity_id}/revoke (master key only)
    GET    /api/admin/agent-identities                      (master key only)

``POST /api/agents/register`` is the first key-minting path in this codebase
that does NOT require a human. It is public because it must be: an agent that
discovers LoopSkill through ``llms.txt`` or an MCP directory has no account, no
browser, and no way to complete an OAuth redirect. It authenticates the CALLER
by Ed25519 proof-of-key instead of by session.

Everything that makes that safe lives in ``app.services.agent_registration`` —
canonical string, signature verification, skew window, replay wall, quota. This
module is a thin translator from that service's typed refusals to HTTP.

The issued key is an ordinary free-tier user key (prefix ``rec_agent_``):
``AuthContext(scope="user", tier=None)``, one active key, free install limits,
``is_sandbox_operator=False``. It reaches search, free installs, bundle
composition/publish, feedback, skill-error reports and registry proposals; it
cannot reach checkout/billing (those routes are JWT-only — an ``x-api-key``
never authenticates there), admin (master-key only) or the sandbox.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import public_origin, settings
from app.database import get_db
from app.models import AgentIdentity
from app.services.agent_registration import (
    MAX_ACTIVE_KEYS_PER_IDENTITY,
    AgentRegistrationError,
    register_agent,
    revoke_identity,
)
from app.utils.client_ip import _real_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


class AgentRegisterIn(BaseModel):
    """The registration payload. Every field except ``contact`` is signed."""

    # Only outer bounds are declared here. The SEMANTIC rules (32 raw pubkey
    # bytes, >= 16 nonce bytes of hex, the skew window) live in
    # app.services.agent_registration so there is exactly one place that decides
    # them — a duplicate min_length here would answer 422 for a case the service
    # documents as a 400 with a named error code.
    pubkey: str = Field(min_length=1, max_length=128, description="base64 raw Ed25519 public key")
    timestamp: str = Field(min_length=1, max_length=64, description="ISO-8601 UTC")
    nonce: str = Field(min_length=1, max_length=256, description="hex, >= 16 bytes")
    agent_name: str = Field(min_length=1, max_length=64)
    contact: str | None = Field(default=None, max_length=128)
    signature: str = Field(min_length=1, max_length=256, description="base64 Ed25519 signature")


class AgentRegisterOut(BaseModel):
    agent_identity_id: str
    agent_name: str
    api_key: str
    key_prefix: str
    scope: str
    tier: str
    capabilities: dict
    endpoints: dict
    warning: str


def _client_ip(request: Request) -> str | None:
    """Resolve the caller's real IP for the per-IP registration cap.

    Uses the SAME trusted-proxy-aware resolver ``RateLimitMiddleware`` uses, so
    a spoofed ``X-Forwarded-For`` from an untrusted peer cannot mint itself a
    fresh per-IP quota bucket.
    """
    ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)
    return None if ip in ("", "unknown") else ip


def _require_master(request: Request) -> None:
    """Master-key gate, identical to ``app/admin_routes.py``'s.

    ``APIKeyMiddleware`` sets ``api_key_user_id = None`` for (and only for) the
    master key. The ``"MISSING"`` default is the fail-closed part: an
    unstamped request is refused rather than mistaken for master.
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")


@router.post("/api/agents/register", response_model=AgentRegisterOut, status_code=201)
def register_agent_route(
    payload: AgentRegisterIn,
    request: Request,
    db: Session = Depends(get_db),
) -> AgentRegisterOut:
    """Self-register an autonomous agent by Ed25519 proof-of-key. No human.

    **The canonical string.** Sign the UTF-8 bytes of, exactly::

        loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}

    with the Ed25519 private key whose public half is ``pubkey``, and send the
    signature base64-encoded. The five substituted values are this request's own
    field values, verbatim, joined by ``:`` — no normalisation is applied on
    either side, so what you sign is what is verified.

    ================  ==========================================================
    field             format
    ================  ==========================================================
    ``pubkey``        standard base64 of the 32 RAW public-key bytes (not PEM)
    ``timestamp``     ISO-8601 UTC, within +/-5 min of server time
    ``nonce``         lowercase hex, >= 16 bytes (32 hex chars); single use
    ``agent_name``    display name, <= 64 chars
    ``contact``       optional, <= 128 chars; NOT part of the signed string
    ``signature``     base64 Ed25519 signature over the canonical string
    ================  ==========================================================

    ``contact`` is excluded from the signature deliberately: it is advisory
    metadata with no authorization weight, and including it would force every
    client to re-sign to correct a typo in an email address.

    Returns 201 with the plaintext key ONCE — it is stored only as a sha256
    hash and cannot be recovered.

    ======  ==================================  =================================
    status  code                                meaning
    ======  ==================================  =================================
    400     invalid_pubkey / invalid_nonce /    malformed field
            invalid_timestamp
    401     invalid_signature                   signature did not verify
    401     timestamp_out_of_range              outside the +/-5 min window
    401     nonce_replayed                      this nonce was already consumed
    409     pubkey_already_registered           known key; the secret is NEVER
                                                re-issued — see ``rotation``
    429     ip_registration_limit /             per-IP or platform-wide 24h cap
            global_registration_limit
    ======  ==================================  =================================

    The issued key is free tier: search, free installs, bundle compose/publish,
    feedback, skill-error reports, registry proposals. It cannot reach
    checkout/billing, admin, or the sandbox.
    """
    try:
        result = register_agent(
            db,
            pubkey=payload.pubkey,
            timestamp=payload.timestamp,
            nonce=payload.nonce,
            agent_name=payload.agent_name,
            signature=payload.signature,
            contact=payload.contact,
            client_ip=_client_ip(request),
        )
    except AgentRegistrationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "detail": exc.message, **exc.extra},
        ) from exc

    origin = public_origin()
    return AgentRegisterOut(
        agent_identity_id=str(result.identity_id),
        agent_name=result.agent_name,
        api_key=result.plaintext_key,
        key_prefix=result.key_prefix,
        scope="user",
        tier="free",
        capabilities={
            "search": True,
            "install_free_skills": True,
            "compose_bundles": True,
            "publish_public_bundles": True,
            "file_feedback": True,
            "report_skill_errors": True,
            "propose_registries": True,
            "run_sandbox": False,
            "checkout_billing": False,
            "admin": False,
            "active_keys": MAX_ACTIVE_KEYS_PER_IDENTITY,
        },
        endpoints={
            "mcp": f"{origin}/api/mcp/http",
            "search": f"{origin}/api/skills/search",
            "install": f"{origin}/api/skills/install",
            "docs": f"{origin}/docs",
            "skill": f"{origin}/skill",
        },
        warning="Save this key now — it is stored only as a hash and will not be shown again.",
    )


# ── Admin: revocation ───────────────────────────────────────────────────────


class AgentIdentityOut(BaseModel):
    id: str
    agent_name: str
    contact: str | None
    revoked: bool
    registration_ip: str | None
    created_at: str | None


def _identity_out(identity: AgentIdentity) -> AgentIdentityOut:
    return AgentIdentityOut(
        id=str(identity.id),
        agent_name=identity.agent_name,
        contact=identity.contact,
        revoked=bool(identity.revoked),
        registration_ip=identity.registration_ip,
        created_at=identity.created_at.isoformat() if identity.created_at else None,
    )


@router.post("/api/admin/agent-identities/{identity_id}/revoke")
def revoke_agent_identity(
    identity_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Revoke a self-registered agent identity. Master key only. Idempotent.

    Sets ``revoked=True`` AND deactivates the identity's API keys. The middleware
    gate (``app/middleware/_agent_identity.py``) reads ``revoked``, so the flag —
    not the key rows — is authoritative and also covers a key minted in a race
    with this call.
    """
    _require_master(request)
    identity = revoke_identity(db, identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="agent_identity_not_found")
    return {"revoked": True, "identity": _identity_out(identity).model_dump()}


@router.get("/api/admin/agent-identities")
def list_agent_identities(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 100,
) -> dict:
    """List self-registered agent identities, newest first. Master key only.

    Never returns key material — there is none to return; keys are stored as
    sha256 hashes and the plaintext existed only in the registration response.
    """
    _require_master(request)
    rows = (
        db.query(AgentIdentity).order_by(AgentIdentity.created_at.desc()).limit(max(1, min(limit, 500))).all()
    )
    return {"identities": [_identity_out(r).model_dump() for r in rows]}
