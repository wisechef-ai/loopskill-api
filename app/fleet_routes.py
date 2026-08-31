"""portal_0610 J3 — HTTP routes for fleet operations.

The fleet logic already exists as MCP tools (app/mcp/tools/fleet.py). J3 exposes
the same four operations over HTTP so the web portal's /fleets surface (and the
AppShell + /home rail, which already call GET /api/fleets) resolve instead of
404ing. This module is a thin HTTP adapter: it resolves an AuthContext from
request state (supporting BOTH a logged-in user via cookie/key AND a rec_fleet_
key whose scope='fleet' auth_ctx is already stamped by the middleware), then
delegates to the existing tool functions — no fleet logic is duplicated.

PM7 (contract-probe-first): the response shapes mirror the MCP tool contracts
exactly (loopskill_fleet_list → {fleets:[{fleet_id,name,subscriptions:[...]}]},
etc.) so the two surfaces never drift.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.mcp.tools.fleet import (
    loopskill_fleet_create,
    loopskill_fleet_list,
    loopskill_fleet_subscribe,
    loopskill_fleet_sync,
)
from app.mcp.tools.placement import loopskill_reconcile_precheck
from app.models import User

# mesh0408e2e W2: entitlement follows subscription STATUS, not the raw tier
# column — a lapsed Pro+ must fall back to free fleet capability.
from app.revenue_truth import entitled_tier_or_free

router = APIRouter(prefix="/api/fleets", tags=["fleets"])


def resolve_fleet_ctx(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """Resolve an AuthContext for a fleet HTTP route.

    Two caller shapes:
      1. rec_fleet_* key — the middleware already stamped a scope='fleet'
         AuthContext on request.state.auth_ctx. Use it as-is.
      2. Logged-in user (cookie/JWT or rec_ key) — the middleware stamped
         request.state.api_key_user_id; build a scope='user' AuthContext.
    A genuinely unauthenticated caller gets 401.
    """
    stamped = getattr(request.state, "auth_ctx", None)
    if stamped is not None and getattr(stamped, "scope", None) in ("fleet", "master"):
        return stamped

    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")

    # master key path (None sentinel) — full access.
    if api_key_user_id is None:
        return AuthContext(scope="master")

    if api_key_user_id in ("MISSING", "CBT_TOKEN"):
        # cbt_ tokens are bundle-scoped, not fleet-capable.
        raise HTTPException(status_code=401, detail="auth_required")

    user = db.query(User).filter(User.id == api_key_user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")

    # activate_0701/TEN: resolve org membership for tenant scope.
    from app.middleware.api_key import _resolve_org_membership

    org_id, is_org_owner = _resolve_org_membership(db, user.id)

    return AuthContext(
        scope="user",
        user_id=user.id,
        # mesh_0408 W4b: carry the calling key's id. Fleet-member enrollment
        # stamps the new member's origin from this key's APIKey.is_test, and a
        # context that dropped the id would silently leave every member
        # unclassified — which reads as "let the beacon slug list decide".
        api_key_id=getattr(request.state, "api_key_id", None),
        # W2: entitled tier, not the raw column — a lapsed subscription must not
        # keep Pro+ fleet capability just because the slug is still on the row.
        tier=entitled_tier_or_free(user),
        org_id=org_id,
        is_org_owner=is_org_owner,
    )


def _raise_for_tool_error(result: dict[str, Any]) -> dict[str, Any]:
    """Map a tool-layer {error: ...} dict to the right HTTP status."""
    err = result.get("error")
    if err is None:
        return result
    status = {
        "forbidden": 403,
        "not_found": 404,
        "fleet_not_found": 404,
        "invalid_fleet_id": 422,
        "invalid_bundle_id": 422,
        "invalid_channel": 422,
        "invalid_org_id": 422,
    }.get(err, 400)
    raise HTTPException(status_code=status, detail=result)


# ── routes ────────────────────────────────────────────────────────────────


class FleetCreateIn(BaseModel):
    name: str
    org_id: str | None = None


@router.get("")
def list_fleets(request: Request, db: Session = Depends(get_db)):
    """GET /api/fleets — list the caller's fleets + subscriptions.

    Mirrors loopskill_fleet_list. The AppShell rail + /home + /fleets page all
    consume this. An anonymous caller gets 401 (the page bounces to /signin).
    """
    ctx = resolve_fleet_ctx(request, db)
    return _raise_for_tool_error(loopskill_fleet_list(db, ctx=ctx))


@router.post("", status_code=201)
def create_fleet(body: FleetCreateIn, request: Request, db: Session = Depends(get_db)):
    """POST /api/fleets — create a named fleet. Returns the plaintext fleet_key ONCE.

    mesh_0408/B' — body.org_id is optional. Omitted → today's behaviour
    (ctx.org_id, the caller's oldest membership). Provided → validated
    against the caller's OWN OrgMembership rows in the tool layer; a caller
    cannot select an org they don't belong to (403), and a malformed UUID
    is rejected (422) before ever reaching the DB query.
    """
    ctx = resolve_fleet_ctx(request, db)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="invalid_name")
    return _raise_for_tool_error(loopskill_fleet_create(db, name=name, ctx=ctx, org_id=body.org_id))


class SubscribeIn(BaseModel):
    cookbook_id: str
    channel: str = "stable"


@router.post("/{fleet_id}/subscribe", status_code=201)
def subscribe_fleet(fleet_id: str, body: SubscribeIn, request: Request, db: Session = Depends(get_db)):
    """POST /api/fleets/{id}/subscribe — subscribe a cookbook on a channel (idempotent)."""
    ctx = resolve_fleet_ctx(request, db)
    return _raise_for_tool_error(
        loopskill_fleet_subscribe(
            db, fleet_id=fleet_id, cookbook_id=body.cookbook_id, channel=body.channel, ctx=ctx
        )
    )


class SyncIn(BaseModel):
    dry_run: bool = False


@router.post("/{fleet_id}/sync")
def sync_fleet_route(fleet_id: str, body: SyncIn, request: Request, db: Session = Depends(get_db)):
    """POST /api/fleets/{id}/sync — sync every subscribed cookbook. dry_run previews."""
    ctx = resolve_fleet_ctx(request, db)
    return _raise_for_tool_error(loopskill_fleet_sync(db, fleet_id=fleet_id, dry_run=body.dry_run, ctx=ctx))


@router.post("/{fleet_id}/reconcile-precheck")
def reconcile_precheck_route(fleet_id: str, request: Request, db: Session = Depends(get_db)):
    """POST /api/fleets/{id}/reconcile-precheck — pre-apply gate for the reconcile step.

    fleetos_1607 gap-close (2026-08-07): re-validates every LIVE placement's
    compatibility (loop requires{} vs member provides{}) BEFORE a reconcile
    applies further placement changes. Manager-capability gated — a bare
    fleet-member key gets 403. Returns {ok: bool, incompatible: [...]} so a
    caller can refuse to proceed on drift instead of silently propagating a
    now-incompatible placement. Mirrors loopskill_reconcile_precheck exactly
    (PM7 — the two surfaces never drift).
    """
    ctx = resolve_fleet_ctx(request, db)
    return _raise_for_tool_error(loopskill_reconcile_precheck(db, fleet_id=fleet_id, ctx=ctx))
