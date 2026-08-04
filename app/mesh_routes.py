"""mesh_0408 T0-D — POST /api/mesh/credentials: mint a mesh credential.

Spec §1: "Each class is minted from the root membership identity (member
API key, over TLS, to LoopSkill) and nothing else." This route is that
trigger. Auth is the caller's OWN FleetMember-dedicated API key — the same
key minted at enrollment by app/fleet_member_routes.py. There is no
separate "a2a_enabled" flag (see app/mesh/mint.py module docstring for why)
and no token-exchange endpoint — each class is minted independently from
this one call, never derived from another mesh credential.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from fastapi import Depends

from app.database import get_db
from app.mesh.constants import VALID_CLASSES
from app.mesh.errors import MeshKeyRingError, MeshMintRaceError, MeshTenantUnassignedError
from app.mesh.mint import mint_credential
from app.models import FleetMember

router = APIRouter(prefix="/api/mesh", tags=["mesh"])


class MintCredentialIn(BaseModel):
    credential_class: Literal["mesh-exec", "mesh-directory", "mesh-admin"]
    target_member_id: str | None = None


def _resolve_calling_member(request: Request, db: Session) -> FleetMember:
    """The caller's OWN FleetMember row, identified by the request's api_key_id.

    401 if the caller did not authenticate with a FleetMember-dedicated key
    (e.g. the account master key, or a user session key with no member row).
    """
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id is None:
        raise HTTPException(status_code=401, detail="mesh_credential_requires_member_key")

    member = (
        db.query(FleetMember)
        .filter(FleetMember.api_key_id == api_key_id, FleetMember.is_active == True)  # noqa: E712
        .first()
    )
    if member is None:
        raise HTTPException(status_code=401, detail="mesh_credential_requires_member_key")
    return member


@router.post("/credentials", status_code=201)
def mint_mesh_credential(
    body: MintCredentialIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Mint one scoped, audience-bound mesh credential for the calling member."""
    if body.credential_class not in VALID_CLASSES:
        raise HTTPException(status_code=422, detail="invalid_credential_class")

    member = _resolve_calling_member(request, db)

    try:
        minted = mint_credential(
            db,
            member_id=member.id,
            cls=body.credential_class,
            target_member_id=body.target_member_id,
        )
    except MeshTenantUnassignedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "mesh_tenant_unassigned", "fleet_id": exc.fleet_id},
        ) from exc
    except MeshMintRaceError as exc:
        raise HTTPException(status_code=400, detail={"error": "mesh_mint_failed", "reason": exc.reason}) from exc
    except MeshKeyRingError as exc:
        raise HTTPException(status_code=503, detail={"error": "mesh_signing_unavailable", "reason": str(exc)}) from exc

    return {
        "token": minted.token,
        "jti": minted.jti,
        "class": minted.cls,
        "aud": minted.aud,
        "exp": minted.exp,
        "iat": minted.iat,
    }
