"""activate_0701 Phase TEN — Org CRUD + membership routes.

Endpoints:
  POST   /api/orgs              create an org + owner membership (201)
  GET    /api/orgs              list orgs the caller is a member of (200)
  POST   /api/orgs/{id}/members add a member to an org (owner-only, 201)
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


def _resolve_user_ctx(request: Request, db: Session) -> tuple[Any, Any]:
    """Resolve the authenticated user from request state.

    Returns (user_id, auth_ctx). Raises 401 if not authenticated.
    """
    auth_ctx = getattr(request.state, "auth_ctx", None)
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")

    if auth_ctx is not None and getattr(auth_ctx, "scope", None) == "master":
        return None, auth_ctx

    if api_key_user_id is not None and api_key_user_id not in ("MISSING", "CBT_TOKEN"):
        return api_key_user_id, auth_ctx

    raise HTTPException(status_code=401, detail="auth_required")


class OrgCreateIn(BaseModel):
    name: str


class OrgMemberAddIn(BaseModel):
    user_id: str
    role: str = "member"


def _generate_org_slug(name: str) -> str:
    """Generate a URL-safe slug from an org name."""
    slug_base = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    slug_base = re.sub(r"-+", "-", slug_base).strip("-")
    if not slug_base:
        slug_base = "org"
    suffix = secrets.token_hex(3)
    return f"{slug_base}-{suffix}"


@router.post("", status_code=201)
def create_org(body: OrgCreateIn, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """POST /api/orgs — create an org + OrgMembership(role='owner').

    The caller becomes the org owner (payer). First org for a user = their
    personal org (the product model: owner owns personal fleet + client fleets).
    """
    from app.models import Org, OrgMembership

    user_id, _ = _resolve_user_ctx(request, db)
    if user_id is None:
        raise HTTPException(status_code=401, detail="auth_required")

    name = (body.name or "").strip()
    if not name or len(name) > 255:
        raise HTTPException(status_code=422, detail="invalid_name")

    slug = _generate_org_slug(name)
    # Ensure slug uniqueness
    while db.query(Org).filter(Org.slug == slug).first() is not None:
        slug = _generate_org_slug(name)

    org = Org(
        id=uuid4(),
        name=name,
        slug=slug,
        api_key_hash=hashlib.sha256(secrets.token_hex(16).encode()).hexdigest(),
    )
    db.add(org)
    db.flush()

    membership = OrgMembership(
        id=uuid4(),
        org_id=org.id,
        user_id=user_id,
        role="owner",
    )
    db.add(membership)
    db.commit()
    db.refresh(org)

    return {
        "org_id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "role": "owner",
    }


@router.get("")
def list_orgs(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """GET /api/orgs — list orgs the caller is a member of."""
    from app.models import OrgMembership

    user_id, _ = _resolve_user_ctx(request, db)
    if user_id is None:
        raise HTTPException(status_code=401, detail="auth_required")

    memberships = db.query(OrgMembership).filter(OrgMembership.user_id == user_id).all()

    org_list = []
    for m in memberships:
        from app.models import Org

        org = db.query(Org).filter(Org.id == m.org_id).first()
        if org is not None:
            org_list.append(
                {
                    "org_id": str(org.id),
                    "name": org.name,
                    "slug": org.slug,
                    "role": m.role,
                }
            )

    return {"orgs": org_list}


@router.post("/{org_id}/members", status_code=201)
def add_org_member(
    org_id: str,
    body: OrgMemberAddIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """POST /api/orgs/{id}/members — add a member (org owner only).

    Accepts user_id directly (email-invite flow is a later sprint).
    """
    from app.models import OrgMembership, User

    caller_id, _ = _resolve_user_ctx(request, db)
    if caller_id is None:
        raise HTTPException(status_code=401, detail="auth_required")

    try:
        org_uuid = UUID(org_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="org_not_found")

    # Verify caller is an owner of this org
    caller_membership = (
        db.query(OrgMembership)
        .filter(
            OrgMembership.org_id == org_uuid,
            OrgMembership.user_id == caller_id,
            OrgMembership.role == "owner",
        )
        .first()
    )
    if caller_membership is None:
        raise HTTPException(status_code=404, detail="org_not_found")

    # Resolve target user
    try:
        target_user_id = UUID(body.user_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_user_id")

    target_user = db.query(User).filter(User.id == target_user_id).first()
    if target_user is None:
        raise HTTPException(status_code=404, detail="user_not_found")

    # Check existing membership
    existing = (
        db.query(OrgMembership)
        .filter(
            OrgMembership.org_id == org_uuid,
            OrgMembership.user_id == target_user_id,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="already_member")

    role = body.role if body.role in ("member", "owner") else "member"
    new_membership = OrgMembership(
        id=uuid4(),
        org_id=org_uuid,
        user_id=target_user_id,
        role=role,
    )
    db.add(new_membership)
    db.commit()
    db.refresh(new_membership)

    return {
        "org_id": str(org_uuid),
        "user_id": str(target_user_id),
        "role": role,
    }
