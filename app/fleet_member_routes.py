"""activate_0701 Phase 1 — HTTP routes for fleet-member enrollment.

Product lock #13: the per-agent API key IS the member identity. One key =
one agent = one FleetMember. Enrollment/removal are FLEET-OWNER actions →
caller scope must be 'user' (fleet owner) or 'master'. A 'fleet'-scope ctx
(rec_fleet_ key) may NOT mint member keys.

Non-owner → 404 (existence never leaks — parity with reconcile-contract §7).

Endpoints:
  POST   /api/fleets/{fleet_id}/members            enroll a new member (201)
  GET    /api/fleets/{fleet_id}/members             keyset-paginated list (200)
  DELETE /api/fleets/{fleet_id}/members/{member_id} deactivate + revoke key (200)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.api_key_routes import _generate_key
from app.database import get_db
from app.fleet_routes import resolve_fleet_ctx
from app.models import APIKey, Fleet, FleetMember, ReconcileEvent

router = APIRouter(prefix="/api/fleets", tags=["fleet-members"])

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

# activate_0701/TEN: tier-based key caps. Member keys (FleetMember) are the
# metered unit — NOT plain user API keys. The cap counts ACTIVE FleetMember
# rows across the caller's org (or personal scope if org_id is NULL).
TIER_KEY_CAPS: dict[str, int] = {
    "free": 1,
    "pro": 200,
    "pro_plus": 200,  # alias for now
}


def _resolve_owned_fleet(db: Session, ctx: Any, fleet_id: str) -> Fleet:
    """Return the Fleet if it exists AND ctx is owner-or-master or same-org.

    Non-owner, non-org-member, and non-existent all resolve to 404
    (no existence leak). A fleet-scope (rec_fleet_) ctx is explicitly
    rejected with 403 — it may read/sync its own fleet elsewhere, but
    member enrollment is a fleet-owner action reserved for the human
    org owner (or master).

    activate_0701/TEN: org-scoped access — a member of the same org as
    the fleet can manage members (the org boundary is the tenant scope).
    """
    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="fleet_not_found")

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        raise HTTPException(status_code=404, detail="fleet_not_found")

    if ctx.scope == "fleet":
        raise HTTPException(status_code=403, detail="fleet_scope_cannot_manage_members")

    is_owner = ctx.scope == "master" or (
        ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == fleet.owner_user_id
    )
    # activate_0701/TEN: same-org members can access org fleets.
    if not is_owner and ctx.org_id is not None and fleet.org_id is not None and ctx.org_id == fleet.org_id:
        is_owner = True
    if not is_owner:
        raise HTTPException(status_code=404, detail="fleet_not_found")

    return fleet


class MemberEnrollIn(BaseModel):
    host: str
    profile: str = "default"
    skills_dir: str


@router.post("/{fleet_id}/members", status_code=201)
def enroll_member(
    fleet_id: str,
    body: MemberEnrollIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enroll one agent as a FleetMember, minting a dedicated API key.

    The plaintext key is returned ONCE. Member-key mint is governed by the
    TIER_KEY_CAPS meter (activate_0701/TEN): free=1, pro/pro_plus=200 member
    keys. The cap counts ACTIVE FleetMember rows across the caller's org
    (or personal scope).
    """
    ctx = resolve_fleet_ctx(request, db)
    fleet = _resolve_owned_fleet(db, ctx, fleet_id)

    host = (body.host or "").strip()
    if not host or len(host) > 255:
        raise HTTPException(status_code=422, detail="invalid_host")
    profile = (body.profile or "default").strip() or "default"
    skills_dir = (body.skills_dir or "").strip()
    if not skills_dir:
        raise HTTPException(status_code=422, detail="invalid_skills_dir")

    existing = (
        db.query(FleetMember)
        .filter(
            FleetMember.fleet_id == fleet.id,
            FleetMember.host == host,
            FleetMember.profile == profile,
            FleetMember.is_active == True,  # noqa: E712
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="member_exists")

    # activate_0701/TEN: tier key cap enforcement (D3 / lock #13).
    # Count ACTIVE FleetMember rows across the caller's org scope (or
    # personal scope if org_id is NULL). Member keys are the metered unit.
    tier = (ctx.tier or "free").lower()
    cap = TIER_KEY_CAPS.get(tier, TIER_KEY_CAPS["free"])

    member_count_q = (
        db.query(func.count(FleetMember.id))
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .filter(FleetMember.is_active == True)  # noqa: E712
    )
    if ctx.org_id is not None:
        member_count_q = member_count_q.filter(Fleet.org_id == ctx.org_id)
    else:
        member_count_q = member_count_q.filter(Fleet.owner_user_id == ctx.user_id)

    current_count = member_count_q.scalar() or 0
    if current_count >= cap:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "tier_key_cap_exceeded",
                "tier": tier,
                "cap": cap,
                "current": current_count,
                "upgrade_url": "/pricing",
            },
        )

    plaintext_key, prefix12, key_hash = _generate_key()
    label = f"member:{host}/{profile}"[:100]
    key_row = APIKey(
        user_id=fleet.owner_user_id,
        key_prefix=prefix12,
        key_hash=key_hash,
        name=label,
        label=label,
        is_active=True,
    )
    db.add(key_row)
    db.flush()

    member = FleetMember(
        fleet_id=fleet.id,
        host=host,
        profile=profile,
        skills_dir=skills_dir,
        api_key_id=key_row.id,
        is_active=True,
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    return {
        "member_id": str(member.id),
        "fleet_id": str(fleet.id),
        "host": member.host,
        "profile": member.profile,
        "skills_dir": member.skills_dir,
        "api_key": plaintext_key,
        "api_key_id": str(key_row.id),
        "key_prefix": prefix12,
        "warning": "Save this key now — it will not be shown again.",
    }


@router.get("/{fleet_id}/members")
def list_members(
    fleet_id: str,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    after: str | None = Query(default=None),
) -> dict[str, Any]:
    """Keyset-paginated list of a fleet's active members.

    Order: (created_at, id) ascending. `last_event_at` is fetched in ONE
    grouped query across the page's member ids (no N+1).
    """
    ctx = resolve_fleet_ctx(request, db)
    fleet = _resolve_owned_fleet(db, ctx, fleet_id)

    q = (
        db.query(FleetMember)
        .filter(FleetMember.fleet_id == fleet.id, FleetMember.is_active == True)  # noqa: E712
        .order_by(FleetMember.created_at.asc(), FleetMember.id.asc())
    )

    if after:
        try:
            after_uuid = UUID(after)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail="invalid_after")
        cursor_member = db.query(FleetMember).filter(FleetMember.id == after_uuid).first()
        if cursor_member is not None:
            # Keyset cursor: skip everything up to and including the cursor member.
            # We use a raw text() predicate because SQLAlchemy's UUID type binding
            # and SQLite's datetime format (no microsecond suffix) make the ORM-level
            # (created_at, id) compound comparison unreliable across SQLite/Postgres.
            # Ordering by (created_at, id) remains in the ORDER BY for stable display;
            # the filter uses id > cursor_id_hex since ids are globally unique.
            cursor_id_hex = str(cursor_member.id).replace("-", "")
            q = q.filter(text("id > :cursor_id_hex")).params(cursor_id_hex=cursor_id_hex)

    # Fetch limit+1 to know whether there's a next page.
    rows = q.limit(limit + 1).all()
    has_more = len(rows) > limit
    page = rows[:limit]

    member_ids = [m.id for m in page]
    last_event_map: dict[UUID, Any] = {}
    if member_ids:
        agg_rows = (
            db.query(ReconcileEvent.member_id, func.max(ReconcileEvent.created_at))
            .filter(ReconcileEvent.member_id.in_(member_ids))
            .group_by(ReconcileEvent.member_id)
            .all()
        )
        for mid, last_at in agg_rows:
            last_event_map[mid] = last_at

    members_out = [
        {
            "member_id": str(m.id),
            "host": m.host,
            "profile": m.profile,
            "skills_dir": m.skills_dir,
            "key_prefix": (db.query(APIKey.key_prefix).filter(APIKey.id == m.api_key_id).scalar()),
            "is_active": m.is_active,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "last_event_at": (
                last_event_map[m.id].isoformat()
                if m.id in last_event_map and last_event_map[m.id] is not None
                else None
            ),
        }
        for m in page
    ]

    return {
        "members": members_out,
        "next_after": str(page[-1].id) if has_more and page else None,
    }


@router.delete("/{fleet_id}/members/{member_id}")
def remove_member(
    fleet_id: str,
    member_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Deactivate a member AND revoke its APIKey. Idempotent (always 200)."""
    ctx = resolve_fleet_ctx(request, db)
    fleet = _resolve_owned_fleet(db, ctx, fleet_id)

    try:
        member_uuid = UUID(member_id)
    except (ValueError, AttributeError):
        return {"removed": True, "member_id": member_id}

    member = (
        db.query(FleetMember).filter(FleetMember.id == member_uuid, FleetMember.fleet_id == fleet.id).first()
    )
    if member is None:
        return {"removed": True, "member_id": member_id}

    if member.is_active:
        member.is_active = False
        key_row = db.query(APIKey).filter(APIKey.id == member.api_key_id).first()
        if key_row is not None:
            key_row.is_active = False
        db.commit()

    return {"removed": True, "member_id": member_id}
