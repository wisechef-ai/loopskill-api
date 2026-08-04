"""mesh_0408 T3-A — LoopSkill's OWN admission verifier for control-plane
credential-bearing endpoints. Spec §1, §2.2, §4.6.

Distinct from ``docs/security/mesh-reference-verifier/verify_mesh_credential.py``:
that module is for THIRD-PARTY receivers who cannot reach LoopSkill's DB and
must verify offline against a locally-cached JWKS snapshot (spec §3).
LoopSkill verifying its own ``loopskill-api`` / ``loopskill-api-admin``
audience is not in that position — it holds the public key ring directly
(``settings.MESH_JWKS_DIR``) AND has live DB access, so admission here
re-derives ``verified_tenant`` fresh from ``Fleet.org_id`` on every call
rather than trusting the token's own (possibly stale, up to §4.1's exposure
window) org claim.

**This is the T3-A load-bearing guarantee, stated precisely:**
``verified_tenant`` is NEVER read from a request body, a self-asserted Agent
Card (there is no Agent Card input on this codepath at all), or even the
verified token's own ``.../claims/org``. It is always the CURRENT
``Fleet.org_id`` for the member the token's ``.../claims/member`` names,
read fresh from the database on every admission decision. A member
deactivated one second ago is rejected one second later — not bounded by
the token's TTL, because the DB read supersedes the claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import jwt

from app.mesh.constants import (
    CLAIM_NS,
    CLASS_TTL_SECONDS,
    HEADER_ALG,
    HEADER_TYP,
    ISS,
    LEEWAY_SECONDS,
)
from app.mesh.errors import MeshVerifyError
from app.mesh.keys import load_public_keys
from app.models import Fleet, FleetMember


@dataclass(frozen=True)
class VerifiedMeshCaller:
    """The outcome of a successful control-plane admission decision.

    ``verified_tenant`` is LoopSkill's own re-derived org UUID — the ONLY
    tenancy fact any T3-A endpoint may trust (spec §2.2 step 3/§4.6).
    """

    member_id: UUID
    verified_tenant: UUID
    fleet_id: UUID
    cls: str
    jti: str


def verify_control_plane_credential(
    token: str,
    *,
    db,
    expected_aud: str,
    expected_class: str,
    jwks_dir: str | None = None,
) -> VerifiedMeshCaller:
    """Verify a mesh credential presented to ONE of LoopSkill's own T3-A
    endpoints, and re-derive the caller's tenant fresh from the DB.

    Raises ``MeshVerifyError`` on ANY failure — bad signature, unknown/
    retired kid, wrong/array audience, wrong or missing class, TTL over the
    class maximum, non-canonical claims, revoked/absent member, or an
    unassigned org. There is no partial success (spec §3.3).
    """
    try:
        hdr = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001 — any header-parse failure rejects
        raise MeshVerifyError("malformed token header") from exc

    if hdr.get("alg") != HEADER_ALG or hdr.get("typ") != HEADER_TYP:
        raise MeshVerifyError("bad header: alg/typ mismatch")

    kid = hdr.get("kid")
    if not kid:
        raise MeshVerifyError("missing kid")

    keys = load_public_keys(jwks_dir)
    key = keys.get(kid)
    if key is None:
        raise MeshVerifyError(f"unknown or retired kid: {kid!r}")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[HEADER_ALG],
            audience=expected_aud,
            issuer=ISS,
            leeway=LEEWAY_SECONDS,
            options={
                "require": ["exp", "iat", "nbf", "aud", "iss", "sub", "jti"],
                "verify_aud": True,
                "verify_exp": True,
                "verify_iss": True,
            },
        )
    except Exception as exc:  # noqa: BLE001 — any decode/verify failure rejects
        raise MeshVerifyError(f"signature/claims verification failed: {exc}") from exc

    # Spec §5 — array audiences are a multi-target credential; PyJWT accepts
    # an array if ANY element matches. Must be enforced explicitly.
    if not isinstance(claims.get("aud"), str):
        raise MeshVerifyError("array audience rejected")

    cls = claims.get(f"{CLAIM_NS}class")
    if cls != expected_class:
        raise MeshVerifyError(f"class mismatch: endpoint requires {expected_class!r}, token carries {cls!r}")

    max_ttl = CLASS_TTL_SECONDS.get(cls)
    if max_ttl is None or claims["exp"] - claims["iat"] > max_ttl + LEEWAY_SECONDS:
        raise MeshVerifyError("ttl exceeds class maximum")

    member_claim = claims.get(f"{CLAIM_NS}member")
    try:
        member_uuid = UUID(str(member_claim))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MeshVerifyError("bad or missing member claim") from exc
    if str(member_uuid) != member_claim:
        raise MeshVerifyError("non-canonical member claim")

    # ── The T3-A guarantee: re-derive tenancy fresh from the DB, never from
    # the token's own org claim. A revoked/re-enrolled/moved member is
    # caught HERE, not by trusting anything the token asserts about itself.
    row = (
        db.query(FleetMember, Fleet)
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .filter(FleetMember.id == member_uuid)
        .first()
    )
    if row is None:
        raise MeshVerifyError("member not found (never existed or hard-deleted)")
    member, fleet = row
    if not member.is_active:
        raise MeshVerifyError("member is not active (revoked)")
    if fleet.org_id is None:
        raise MeshVerifyError("fleet has no assigned org (mesh_tenant_unassigned)")

    return VerifiedMeshCaller(
        member_id=member.id,
        verified_tenant=fleet.org_id,
        fleet_id=fleet.id,
        cls=cls,
        jti=claims["jti"],
    )
