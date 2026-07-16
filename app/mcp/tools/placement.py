"""fleetos_1607 Phase A — placement MCP tools (the manager surface).

The fleet-manager KEY CAPABILITY (§0 #9) exposed over MCP. Every tool here is
gated by ``authz.can_manage_fleet`` — a bare fleet-member key gets 403, an
operator/owner/master key gets through. These wrap the epoch-CAS transitions in
``app.services.placement`` and return structured, honest results (including the
per-safety-class duplicate-risk text on a force move).

Tools:
  loopskill_assign        — place a loop on a member (capability preflight first)
  loopskill_evacuate      — remove a loop's placement entirely
  loopskill_placements    — read current placements for a fleet
  loopskill_force_move    — move a loop off a presumed-dead host (duplicate risk)
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.mcp.tools.fleet_write import _NOT_HANDLED
from app.models import Fleet, FleetMember, LoopManifest, LoopPlacement
from app.services import placement as placement_svc

logger = logging.getLogger(__name__)

# Per-safety-class consequence text surfaced VERBATIM in a force-move approval
# (§0 #11). A force move onto a possibly-still-alive host cannot promise no
# double-fire; the honest text depends on the loop's declared safety_class.
_FORCE_MOVE_CONSEQUENCE: dict[str, str] = {
    "idempotent": (
        "safety_class=idempotent: a duplicate fire is safe to repeat — the loop's "
        "effect is the same whether it runs once or twice. Force move is low-risk."
    ),
    "best-effort": (
        "safety_class=best-effort: a duplicate fire may cause a redundant side effect "
        "(a second message, a second write). Tolerable but not free. Proceed knowingly."
    ),
    "manual-only": (
        "safety_class=manual-only: this loop must NOT auto-fire twice. If the old host "
        "is only partitioned (not dead) a force move can cause a real double effect. "
        "Confirm the old host is genuinely down before proceeding."
    ),
    "fenced": (
        "safety_class=fenced: fire-time fencing is a v2 feature (not enforced yet). "
        "Until then this loop is treated as manual-only for force-move purposes."
    ),
}


def _resolve_fleet(db: Session, fleet_id: str) -> Fleet | None:
    try:
        fid = UUID(fleet_id)
    except (ValueError, AttributeError):
        return None
    return db.query(Fleet).filter(Fleet.id == fid).first()


def _forbidden() -> dict[str, Any]:
    return {"error": "forbidden", "code": 403, "detail": "fleet-manager capability required"}


def loopskill_assign(
    db: Session,
    fleet_id: str,
    loop_key: str,
    member_id: str,
    op_id: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Assign a loop to a fleet member. Manager-capability gated.

    Runs the capability/secret preflight; on a miss returns a structured refusal
    naming the missing requirements (never a silent placement onto an incapable
    member).
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    fleet = _resolve_fleet(db, fleet_id)
    if fleet is None:
        return {"error": "fleet_not_found", "code": 404}
    if not authz.can_manage_fleet(ctx, fleet):
        return _forbidden()
    try:
        mid = UUID(member_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_member_id", "code": 422}
    member = db.query(FleetMember).filter(FleetMember.id == mid, FleetMember.fleet_id == fleet.id).first()
    if member is None:
        return {"error": "member_not_found", "code": 404}

    try:
        pl = placement_svc.assign(db, fleet.id, loop_key, mid, op_id=op_id)
    except placement_svc.PlacementError as exc:
        code = 409 if exc.code in ("already_placed", "preflight_failed") else 422
        return {"error": exc.code, "code": code, "detail": exc.message, **exc.extra}
    return {
        "assigned": True,
        "placement_id": str(pl.id),
        "loop_key": loop_key,
        "member_id": member_id,
        "epoch": pl.placement_epoch,
        "status": pl.status,
    }


def loopskill_evacuate(
    db: Session,
    fleet_id: str,
    loop_key: str,
    op_id: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Remove a loop's placement entirely. Manager-capability gated."""
    if ctx is None:
        ctx = AuthContext(scope="master")
    fleet = _resolve_fleet(db, fleet_id)
    if fleet is None:
        return {"error": "fleet_not_found", "code": 404}
    if not authz.can_manage_fleet(ctx, fleet):
        return _forbidden()

    removed = placement_svc.evacuate(db, fleet.id, loop_key, op_id=op_id)
    if removed is None:
        return {"evacuated": False, "detail": "no live placement for loop", "loop_key": loop_key}
    return {"evacuated": True, "loop_key": loop_key, "final_epoch": removed.placement_epoch}


def loopskill_placements(
    db: Session,
    fleet_id: str,
    include_removed: bool = False,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Read current placements for a fleet. Manager-capability gated."""
    if ctx is None:
        ctx = AuthContext(scope="master")
    fleet = _resolve_fleet(db, fleet_id)
    if fleet is None:
        return {"error": "fleet_not_found", "code": 404}
    if not authz.can_manage_fleet(ctx, fleet):
        return _forbidden()

    q = db.query(LoopPlacement).filter(LoopPlacement.fleet_id == fleet.id)
    if not include_removed:
        q = q.filter(LoopPlacement.status != "removed")
    rows = q.order_by(LoopPlacement.loop_key, LoopPlacement.placement_epoch.desc()).all()
    return {
        "fleet_id": fleet_id,
        "count": len(rows),
        "placements": [
            {
                "placement_id": str(r.id),
                "loop_key": r.loop_key,
                "member_id": str(r.member_id),
                "status": r.status,
                "epoch": r.placement_epoch,
                "forced": r.forced,
            }
            for r in rows
        ],
    }


def loopskill_force_move(
    db: Session,
    fleet_id: str,
    loop_key: str,
    new_member_id: str,
    acknowledge_duplicate_risk: bool = False,
    op_id: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Force a loop off a presumed-dead host. Manager-capability gated.

    This is the honest dangerous path (§0 #11). It REFUSES unless the caller
    passes acknowledge_duplicate_risk=True, and it returns the per-safety-class
    consequence text so the risk is surfaced verbatim, not buried.
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    fleet = _resolve_fleet(db, fleet_id)
    if fleet is None:
        return {"error": "fleet_not_found", "code": 404}
    if not authz.can_manage_fleet(ctx, fleet):
        return _forbidden()

    manifest = db.query(LoopManifest).filter(LoopManifest.loop_id == loop_key).first()
    safety_class = (manifest.safety_class if manifest else "best-effort") or "best-effort"
    consequence = _FORCE_MOVE_CONSEQUENCE.get(safety_class, _FORCE_MOVE_CONSEQUENCE["best-effort"])

    if not acknowledge_duplicate_risk:
        return {
            "error": "duplicate_risk_not_acknowledged",
            "code": 428,  # Precondition Required
            "loop_key": loop_key,
            "safety_class": safety_class,
            "consequence": consequence,
            "detail": "re-call with acknowledge_duplicate_risk=true to proceed",
        }

    try:
        nid = UUID(new_member_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_member_id", "code": 422}

    try:
        pl = placement_svc.force_move(db, fleet.id, loop_key, nid, op_id=op_id)
    except placement_svc.PlacementError as exc:
        code = 409 if exc.code == "preflight_failed" else 422
        return {"error": exc.code, "code": code, "detail": exc.message, **exc.extra}
    return {
        "force_moved": True,
        "placement_id": str(pl.id),
        "loop_key": loop_key,
        "new_member_id": new_member_id,
        "epoch": pl.placement_epoch,
        "forced": True,
        "safety_class": safety_class,
        "consequence": consequence,
    }


# Sentinel returned by dispatch when this module doesn't handle the tool name.
# Shared with fleet_write so a single delegated-dispatch chain can compare
# against one sentinel object across every handler.
# Tool-name → handler. Matches the fleet_write.dispatch_f1 delegated-dispatch
# pattern so the shared dispatch chain wires the placement surface.
_PLACEMENT_TOOLS = {
    "loopskill_assign",
    "loopskill_evacuate",
    "loopskill_placements",
    "loopskill_force_move",
}


def dispatch_placement(name: str, db: Session, args: dict[str, Any], ctx: AuthContext) -> Any:
    """Delegated dispatch for the Phase A placement/manager tools.

    Returns ``_NOT_HANDLED`` when ``name`` is not a placement tool, so the
    server's dispatch chain can fall through to the next handler.
    """
    if name not in _PLACEMENT_TOOLS:
        return _NOT_HANDLED
    if name == "loopskill_assign":
        return loopskill_assign(
            db,
            fleet_id=args.get("fleet_id", ""),
            loop_key=args.get("loop_key", ""),
            member_id=args.get("member_id", ""),
            op_id=args.get("op_id"),
            ctx=ctx,
        )
    if name == "loopskill_evacuate":
        return loopskill_evacuate(
            db,
            fleet_id=args.get("fleet_id", ""),
            loop_key=args.get("loop_key", ""),
            op_id=args.get("op_id"),
            ctx=ctx,
        )
    if name == "loopskill_placements":
        return loopskill_placements(
            db,
            fleet_id=args.get("fleet_id", ""),
            include_removed=bool(args.get("include_removed", False)),
            ctx=ctx,
        )
    # loopskill_force_move
    return loopskill_force_move(
        db,
        fleet_id=args.get("fleet_id", ""),
        loop_key=args.get("loop_key", ""),
        new_member_id=args.get("new_member_id", ""),
        acknowledge_duplicate_risk=bool(args.get("acknowledge_duplicate_risk", False)),
        op_id=args.get("op_id"),
        ctx=ctx,
    )
