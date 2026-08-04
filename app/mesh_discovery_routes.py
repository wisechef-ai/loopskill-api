"""mesh_0408 T3-A — LoopSkill as A2A discovery authority.

Spec §1/§2.2/§4.6, plan §3 T3-A.2/T3-A.3.

  GET /api/orgs/{org_id}/a2a-directory

The tenant unit for A2A discovery is `org_id`, NOT `fleet_id` (an org can
own several fleets; a peer agent looking for "who can I reach in this
tenant" needs the org-wide view, not one fleet's slice).

Auth: a `mesh-directory` credential (`aud: loopskill-api`), presented as
`Authorization: Bearer <token>`. This is deliberately NOT the existing
`x-api-key` scheme — mesh credentials are audience-bound JWTs, not opaque
API keys, and mixing the two auth schemes on one header would blur the two
credential universes spec §0's docstring (app/mesh/keys.py) already warns
against merging.

**verified_tenant (T3-A.3, the whole point of this phase):** the caller's
org is NEVER read from the token's own `.../claims/org` here — it is
re-derived by `verify_control_plane_credential` fresh from `Fleet.org_id`
for the CURRENT DB state of the member the token names. A token minted 10
minutes ago naming a member who has since been revoked, re-enrolled (new
UUID — old token is now an orphan reference), or moved to another org is
caught HERE, at the DB read, not by trusting anything the JWT payload says
about itself. That re-derivation is what makes cross-org access impossible
even for a technically-still-valid (unexpired, correctly signed) token: the
path is is-this-token-well-formed (JWT layer) AND
does-the-live-DB-agree-this-member-is-in-this-org (this module), both
required, JWT alone is not authoritative.

Endpoint registration reuses `FleetMemberLiveness.provides` (T3-A.4) — no
parallel directory table. A member's advertised A2A endpoint lives at
`provides["a2a"]` (already the schema `loopskill_ping` writes to, see
app/mcp/tools/fleet_ingest.py) and nothing else stores it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.mesh.constants import CLASS_MESH_DIRECTORY, DIRECTORY_AUD
from app.mesh.errors import MeshVerifyError
from app.mesh.verify import verify_control_plane_credential
from app.models import Fleet, FleetMember, FleetMemberLiveness

router = APIRouter(prefix="/api/orgs", tags=["mesh", "a2a-directory"])


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="mesh_credential_required")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="mesh_credential_required")
    return token


@router.get("/{org_id}/a2a-directory")
def a2a_directory(
    org_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List A2A-reachable members of ONE org, gated by a mesh-directory credential.

    Rejects (401) a missing/malformed/wrong-audience/wrong-class/expired
    credential. Rejects (403) a credential whose LoopSkill-verified tenant
    (re-derived from the live DB, never the token's own claim) does not
    match the requested `org_id` path segment — the cross-tenant gate.
    """
    try:
        org_uuid = UUID(org_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="org_not_found")

    token = _bearer_token(request)
    try:
        caller = verify_control_plane_credential(
            token,
            db=db,
            expected_aud=DIRECTORY_AUD,
            expected_class=CLASS_MESH_DIRECTORY,
        )
    except MeshVerifyError as exc:
        raise HTTPException(
            status_code=401, detail={"error": "mesh_credential_invalid", "reason": exc.reason}
        ) from exc

    # THE cross-tenant gate — verified_tenant came from live DB re-derivation,
    # not from the token's own org claim (spec §2.2/§4.6, T3-A.3).
    if caller.verified_tenant != org_uuid:
        raise HTTPException(status_code=403, detail="cross_tenant_rejected")

    rows = (
        db.query(FleetMember, FleetMemberLiveness)
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .outerjoin(FleetMemberLiveness, FleetMemberLiveness.member_id == FleetMember.id)
        .filter(Fleet.org_id == org_uuid, FleetMember.is_active == True)  # noqa: E712
        .order_by(FleetMember.created_at.asc(), FleetMember.id.asc())
        .all()
    )

    entries: list[dict[str, Any]] = []
    for member, liveness in rows:
        provides = dict(liveness.provides or {}) if liveness is not None else {}
        endpoint = provides.get("a2a")
        if not endpoint:
            continue  # T3-A.2 — only members that have actually registered an A2A endpoint
        last_ping_iso = None
        if liveness is not None and liveness.last_ping_at:
            last_ping_iso = liveness.last_ping_at.isoformat()
        entries.append(
            {
                "member_id": str(member.id),
                "fleet_id": str(member.fleet_id),
                "host": member.host,
                "profile": member.profile,
                "a2a_endpoint": endpoint,
                "last_ping_at": last_ping_iso,
            }
        )

    return {"org_id": str(org_uuid), "members": entries}
