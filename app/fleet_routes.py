"""portal_0610 J3 — HTTP routes for fleet operations.

The fleet logic already exists as MCP tools (app/mcp/tools/fleet.py). J3 exposes
the same four operations over HTTP so the web portal's /fleets surface (and the
AppShell + /home rail, which already call GET /api/fleets) resolve instead of
404ing. This module is a thin HTTP adapter: it resolves an AuthContext from
request state (supporting BOTH a logged-in user via cookie/key AND a rec_fleet_
key whose scope='fleet' auth_ctx is already stamped by the middleware), then
delegates to the existing tool functions — no fleet logic is duplicated.

PM7 (contract-probe-first): the response shapes mirror the MCP tool contracts
exactly (recipes_fleet_list → {fleets:[{fleet_id,name,subscriptions:[...]}]},
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
    recipes_fleet_create,
    recipes_fleet_list,
    recipes_fleet_subscribe,
    recipes_fleet_sync,
)
from app.models import User

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
        # cbt_ tokens are cookbook-scoped, not fleet-capable.
        raise HTTPException(status_code=401, detail="auth_required")

    user = db.query(User).filter(User.id == api_key_user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")

    return AuthContext(
        scope="user",
        user_id=user.id,
        tier=user.subscription_tier or "free",
    )


def _raise_for_tool_error(result: dict[str, Any]) -> dict[str, Any]:
    """Map a tool-layer {error: ...} dict to the right HTTP status."""
    err = result.get("error")
    if err is None:
        return result
    status = {
        "forbidden": 403,
        "not_found": 404,
        "invalid_fleet_id": 422,
        "invalid_cookbook_id": 422,
        "invalid_channel": 422,
    }.get(err, 400)
    raise HTTPException(status_code=status, detail=result)


# ── routes ────────────────────────────────────────────────────────────────


class FleetCreateIn(BaseModel):
    name: str


@router.get("")
def list_fleets(request: Request, db: Session = Depends(get_db)):
    """GET /api/fleets — list the caller's fleets + subscriptions.

    Mirrors recipes_fleet_list. The AppShell rail + /home + /fleets page all
    consume this. An anonymous caller gets 401 (the page bounces to /signin).
    """
    ctx = resolve_fleet_ctx(request, db)
    return _raise_for_tool_error(recipes_fleet_list(db, ctx=ctx))


@router.post("", status_code=201)
def create_fleet(body: FleetCreateIn, request: Request, db: Session = Depends(get_db)):
    """POST /api/fleets — create a named fleet. Returns the plaintext fleet_key ONCE."""
    ctx = resolve_fleet_ctx(request, db)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="invalid_name")
    return _raise_for_tool_error(recipes_fleet_create(db, name=name, ctx=ctx))


class SubscribeIn(BaseModel):
    cookbook_id: str
    channel: str = "stable"


@router.post("/{fleet_id}/subscribe", status_code=201)
def subscribe_fleet(fleet_id: str, body: SubscribeIn, request: Request, db: Session = Depends(get_db)):
    """POST /api/fleets/{id}/subscribe — subscribe a cookbook on a channel (idempotent)."""
    ctx = resolve_fleet_ctx(request, db)
    return _raise_for_tool_error(
        recipes_fleet_subscribe(
            db, fleet_id=fleet_id, cookbook_id=body.cookbook_id, channel=body.channel, ctx=ctx
        )
    )


class SyncIn(BaseModel):
    dry_run: bool = False


@router.post("/{fleet_id}/sync")
def sync_fleet_route(fleet_id: str, body: SyncIn, request: Request, db: Session = Depends(get_db)):
    """POST /api/fleets/{id}/sync — sync every subscribed cookbook. dry_run previews."""
    ctx = resolve_fleet_ctx(request, db)
    return _raise_for_tool_error(recipes_fleet_sync(db, fleet_id=fleet_id, dry_run=body.dry_run, ctx=ctx))
