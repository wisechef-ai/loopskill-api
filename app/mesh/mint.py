"""mesh_0408 T0-D — credential minting. Spec §1, §2, §2.4, §4.9.

**Minting trigger — resolves the plan gap (spec §12.6, plan §3 T0-D.3).**

Plan §3 T0-D.3 said credentials are minted "at member enrollment
(`a2a_enabled`)". Verified against `app/models.py`: no `a2a_enabled` column
exists on `FleetMember`, and no such identifier appears anywhere in the
codebase. Adding a new boolean gate column would be inventing scope the
spec does not ask for.

**Decision: name the real trigger instead of adding a column.** Spec §1
already specifies the real trigger precisely: *"Each class is minted from
the root membership identity (member API key, over TLS, to LoopSkill) and
nothing else."* That is an ON-DEMAND, authenticated mint call — not a flag
flipped at enrollment time. `mint_credential()` below is called from
`POST /api/mesh/credentials` (app/mesh_routes.py), authenticated by the
caller's OWN FleetMember API key (the same key issued at enrollment in
`app/fleet_member_routes.py`). The member's key IS its root membership
identity; possessing it is the enrollment-time capability the plan gestured
at. No new column, no new enablement flag, no derivation/exchange endpoint.

**Transactional read (spec §4.9).** `Fleet` and `FleetMember` are read with
ONE joined query so a concurrent org-move or member-revocation cannot be
observed half-applied (no separate SELECT that could straddle a commit
under READ COMMITTED). A mint whose read sees a revoked or moved member
fails outright rather than signing from a stale/half view.

**Fail-closed on unassigned org (spec §2.4).** `Fleet.org_id IS NULL` means
"personal scope" and is legitimate for non-mesh usage — but a mint that
issued `org: null` credentials would make every tenantless member mutually
entitled (`null == null`). `mint_credential()` raises
`MeshTenantUnassignedError` in that case; the route layer maps it to
HTTP 409 `mesh_tenant_unassigned`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from uuid import UUID

import jwt
from sqlalchemy.orm import Session

from app.mesh.constants import (
    ADMIN_AUD,
    CLAIM_NS,
    CLASS_MESH_ADMIN,
    CLASS_MESH_DIRECTORY,
    CLASS_MESH_EXEC,
    CLASS_TTL_SECONDS,
    DIRECTORY_AUD,
    HEADER_ALG,
    HEADER_TYP,
    ISS,
    VALID_CLASSES,
)
from app.mesh.errors import MeshMintRaceError, MeshTenantUnassignedError
from app.mesh.keys import SigningKey, load_signing_key
from app.mesh.ulid import new_ulid
from app.models import Fleet, FleetMember

_mint_audit_logger = logging.getLogger("loopskill.mesh.mint_audit")


def _canonical_uuid(value) -> str:
    """Spec §7 — canonical UUID form: lowercase, hyphenated, RFC 4122."""
    return str(UUID(str(value)))


@dataclass(frozen=True)
class MintedCredential:
    token: str
    jti: str
    org: str
    fleet: str
    member: str
    cls: str
    aud: str
    exp: int
    iat: int


def _resolve_member_and_fleet(db: Session, member_id) -> tuple[FleetMember, Fleet]:
    """ONE joined query — spec §4.9's single-transaction membership read."""
    try:
        member_uuid = UUID(str(member_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MeshMintRaceError(f"invalid member id: {member_id!r}") from exc

    row = (
        db.query(FleetMember, Fleet)
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .filter(FleetMember.id == member_uuid)
        .first()
    )
    if row is None:
        raise MeshMintRaceError(f"member {member_uuid} not found")
    member, fleet = row
    if not member.is_active:
        raise MeshMintRaceError(f"member {member_uuid} is not active (revoked/moved mid-mint)")
    return member, fleet


def mint_credential(
    db: Session,
    *,
    member_id,
    cls: str,
    target_member_id: str | None = None,
    signing_key: SigningKey | None = None,
) -> MintedCredential:
    """Mint one scoped, audience-bound mesh credential. Spec §1, §2.

    Args:
        member_id: the FleetMember minting FOR (the root membership identity
            — the caller authenticated with this member's own API key).
        cls: one of mesh-exec / mesh-directory / mesh-admin.
        target_member_id: REQUIRED for mesh-exec (the receiving peer's member
            id, becomes `aud`). Ignored for mesh-directory/mesh-admin, whose
            audiences are the fixed literals loopskill-api /
            loopskill-api-admin.
        signing_key: injection point for tests; defaults to
            ``load_signing_key()`` (spec §0.3 custody rules apply there).

    Raises:
        MeshTenantUnassignedError: Fleet.org_id IS NULL — fail closed (409).
        MeshMintRaceError: member not found, inactive, or bad class/target.
    """
    if cls not in VALID_CLASSES:
        raise MeshMintRaceError(f"unknown credential class: {cls!r}")

    member, fleet = _resolve_member_and_fleet(db, member_id)

    # Spec §2.4 — fail closed on nullable org_id. Never mint org: null.
    if fleet.org_id is None:
        raise MeshTenantUnassignedError(str(fleet.id))

    if cls == CLASS_MESH_EXEC:
        if not target_member_id:
            raise MeshMintRaceError("mesh-exec mint requires target_member_id (the receiving peer)")
        try:
            aud = f"lsm:member:{_canonical_uuid(target_member_id)}"
        except ValueError as exc:
            raise MeshMintRaceError(f"invalid target_member_id: {target_member_id!r}") from exc
    elif cls == CLASS_MESH_DIRECTORY:
        aud = DIRECTORY_AUD
    else:  # CLASS_MESH_ADMIN
        aud = ADMIN_AUD

    key = signing_key if signing_key is not None else load_signing_key()

    now = int(time.time())
    ttl = CLASS_TTL_SECONDS[cls]
    jti = new_ulid()

    org_str = _canonical_uuid(fleet.org_id)
    fleet_str = _canonical_uuid(fleet.id)
    member_str = _canonical_uuid(member.id)

    claims = {
        "iss": ISS,
        "sub": f"lsm:member:{member_str}",
        "aud": aud,
        "exp": now + ttl,
        "iat": now,
        "nbf": now,
        "jti": jti,
        f"{CLAIM_NS}org": org_str,
        f"{CLAIM_NS}fleet": fleet_str,
        f"{CLAIM_NS}member": member_str,
        f"{CLAIM_NS}class": cls,
        f"{CLAIM_NS}pact": None,  # spec §10 — reserved settlement slot, always null
    }

    token = jwt.encode(
        claims,
        key.private_key,
        algorithm=HEADER_ALG,
        headers={"kid": key.kid, "typ": HEADER_TYP},
    )

    # Spec §6 — mint events must be logged: jti, org, class, member, timestamp.
    # Never the token itself (that's the sole redaction rule this call site
    # must honour — see docs/security/mesh-credential-audit.md).
    _mint_audit_logger.info(
        "mesh_mint",
        extra={
            "mesh_jti": jti,
            "mesh_org": org_str,
            "mesh_class": cls,
            "mesh_member": member_str,
            "mesh_aud": aud,
            "mesh_iat": now,
        },
    )

    return MintedCredential(
        token=token,
        jti=jti,
        org=org_str,
        fleet=fleet_str,
        member=member_str,
        cls=cls,
        aud=aud,
        exp=now + ttl,
        iat=now,
    )
