"""feat/composite-loop-deploy — POST /api/composite-loops/{slug}/deploy.

The portal deploy button. Wires the EXISTING placement chain (fleetos_1607
Phase I `loopskill_declare_loop` + Phase A `placement.assign`) onto the NEW
composite-loop registry (loopskill_activate_0701 Phase A2): a logged-in
fleet manager deploys a published composite loop to one of their fleet
members, and the member's next sync tick (GET /api/my/loop-assignments)
materializes it as a local Hermes cron.

Kept in its own module (not composite_loop_routes.py) to respect the
600-line pyfile-size gate — this is a NEW file and MUST be registered in
``tests/_app_factory.py`` ``_ROUTER_SPECS`` (an earlier PR missed this and
every auth test 404'd).

No new logic is invented for the manifest upsert or the placement write —
both are delegated verbatim to the existing services so the scope-stamping
and epoch-CAS semantics never drift from the MCP surface:

  * ``app.mcp.tools.fleet_ingest.loopskill_declare_loop`` — upserts the
    LoopManifest (owner_user_id + org_id BOTH stamped from the fleet).
  * ``app.services.placement.assign`` — epoch-guarded placement write with
    capability preflight.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.database import get_db
from app.mcp.tools.fleet_ingest import loopskill_declare_loop
from app.models import CompositeLoop, Fleet, FleetMember
from app.services import placement as placement_svc

router = APIRouter(tags=["composite-loops"])


class CompositeLoopDeployIn(BaseModel):
    fleet_id: str
    member_id: str


def _resolve_deploy_ctx(request: Request) -> AuthContext:
    """Resolve an AuthContext for the deploy route.

    ``/api/composite-loops`` is a PUBLIC_PREFIXES path (browse/detail are
    intentionally anonymous-readable), so APIKeyMiddleware stamps
    ``request.state.auth_ctx`` opportunistically for EVERY request on this
    prefix (master / user-scope-from-rec_-key / cookie-derived / anonymous)
    but does NOT populate ``request.state.api_key_user_id`` the way
    JWT_AUTH_PREFIXES routes do — so this handler reads the stamped
    ``auth_ctx`` directly (mirrors ``composite_loop_routes.publish_composite_loop``)
    rather than ``fleet_routes.resolve_fleet_ctx``, which depends on
    ``api_key_user_id`` and would 401 every caller on this prefix.
    """
    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None or getattr(ctx, "scope", None) in (None, "anonymous"):
        raise HTTPException(status_code=401, detail="auth_required")
    return ctx


def _not_deployable(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"reason": "not_deployable", "detail": detail})


def _resolve_deployable_manifest(cl: CompositeLoop) -> dict[str, Any]:
    """Return the deployable manifest dict, or raise 409 not_deployable.

    Source order: latest version's manifest when one exists, else the
    CompositeLoop ROW itself — schedule/prompt/skills are NOT NULL columns on
    the row (the publish surface writes them there), and seeded/v0 loops like
    'atomic-habits' legitimately have versions=[] while being fully runnable.
    Requiring a version row would 409 every such loop (live-found pre-merge).

    The minimum bar for a "runnable" LoopManifest is a non-empty schedule +
    prompt (mirrors LoopManifest's own NOT NULL contract on those columns).
    """
    if cl.versions:
        manifest = dict(cl.versions[0].manifest or {})
    else:
        manifest = {
            "schedule": cl.schedule,
            "prompt": getattr(cl, "prompt", None),
            "skills": list(cl.skills or []),
        }
    if not str(manifest.get("schedule") or "").strip():
        raise _not_deployable("composite loop has no schedule (row or version)")
    if not str(manifest.get("prompt") or "").strip():
        raise _not_deployable("composite loop has no prompt (row or version)")
    return manifest


def _parse_uuid_or_404(value: str, *, not_found_detail: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail=not_found_detail) from None


@router.post("/api/composite-loops/{slug}/deploy")
def deploy_composite_loop(
    slug: str,
    body: CompositeLoopDeployIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Deploy a published composite loop to one fleet member.

    Upserts a LoopManifest (declare_loop semantics, loop_id=slug) and creates
    (or idempotently replays) a placement (assign semantics, with capability
    preflight). The member's next sync tick reads the placement + manifest
    via GET /api/my/loop-assignments and materializes it as a local cron.
    """
    ctx = _resolve_deploy_ctx(request)

    cl = db.query(CompositeLoop).filter(CompositeLoop.slug == slug).first()
    if cl is None or cl.is_archived:
        raise HTTPException(status_code=404, detail="composite_loop_not_found")

    fleet_uuid = _parse_uuid_or_404(body.fleet_id, not_found_detail="fleet_not_found")
    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        raise HTTPException(status_code=404, detail="fleet_not_found")

    if not authz.can_manage_fleet(ctx, fleet):
        raise HTTPException(status_code=403, detail="not_fleet_manager")

    member_uuid = _parse_uuid_or_404(body.member_id, not_found_detail="member_not_found")
    member = (
        db.query(FleetMember).filter(FleetMember.id == member_uuid, FleetMember.fleet_id == fleet.id).first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="member_not_found")

    manifest = _resolve_deployable_manifest(cl)

    skills_payload = [
        {"id": s.get("slug")} for s in (manifest.get("skills") or []) if isinstance(s, dict) and s.get("slug")
    ]

    declare_result = loopskill_declare_loop(
        db,
        str(fleet.id),
        slug,
        str(manifest.get("schedule")),
        str(manifest.get("prompt")),
        skills=skills_payload,
        requires={},
        secret_refs=[],
        model=manifest.get("model"),
        deliver=None,
        safety_class="best-effort",
        state_class="stateless",
        concurrency_policy="forbid",
        ctx=ctx,
    )
    if declare_result.get("error"):
        # Defensive: composite-loop data passed our own 409 gate above but the
        # shared ingest validator still refused it. Never a bare 500.
        raise _not_deployable(str(declare_result["error"]))

    # Stable op_id per (fleet, loop, member) triple — a re-deploy of the same
    # loop to the same member replays the existing placement (idempotent),
    # rather than colliding with the "already_placed" transition guard.
    op_id = f"composite-deploy:{fleet.id}:{slug}:{member.id}"
    try:
        placement = placement_svc.assign(
            db,
            fleet.id,
            slug,
            member.id,
            op_id=op_id,
        )
    except placement_svc.PlacementError as exc:
        if exc.code == "preflight_failed":
            missing = exc.extra.get("missing", [])
            raise HTTPException(
                status_code=400,
                detail={"reason": ", ".join(missing) or "preflight_failed", "missing": missing},
            ) from exc
        raise HTTPException(
            status_code=409,
            detail={"reason": exc.code, "detail": exc.message},
        ) from exc

    return {
        "deployed": True,
        "loop_id": slug,
        "fleet_id": str(fleet.id),
        "member_id": str(member.id),
        "placement_id": str(placement.id),
        "epoch": placement.placement_epoch,
        "status": placement.status,
        "note": "member applies on its next sync tick",
    }
