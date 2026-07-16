"""fleetos_1607 Phase I — the ingest surface (the missing write path).

Phases 0/A shipped the LoopManifest + FleetMemberLiveness SCHEMA and the
read/compute/authz layers, but NO agent-facing tool ever WROTE those rows — so
the placement chain was inert by construction (a member can never pass
`preflight_member` without a liveness row; `assign` can never fire without a
manifest to place). This module closes that gap:

* ``loopskill_ping``          — a member advertises liveness + typed provides{}.
* ``loopskill_declare_loop``  — a fleet declares/updates a loop's desired-state
                                manifest (upsert by (owner-scope, loop_id)).

Both are gated to the fleet OWNER (or master). A bare member key cannot declare
loops for the fleet — that stays the owner/manager surface.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.mcp.tools.fleet_write import _NOT_HANDLED
from app.models import Fleet, FleetMember, FleetMemberLiveness, LoopManifest

_VALID_SAFETY = ("idempotent", "best-effort", "manual-only")
_VALID_STATE = ("stateless", "external", "local-resettable", "local-required")
_VALID_CONCURRENCY = ("forbid", "allow", "replace")


def _can_manage(db: Session, ctx: AuthContext, fleet: Fleet | None) -> bool:
    """Owner / manager-capability / master gate for the ingest surface (Phase A)."""
    if fleet is None:
        return False
    return authz.can_manage_fleet(ctx, fleet)


def loopskill_ping(
    db: Session,
    member_id: str,
    provides: dict[str, Any] | None = None,
    reconcile_interval_seconds: int | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Advertise a member's liveness + typed capabilities (upsert).

    This is the OPERATIONAL heartbeat the manager surface reads: it stamps
    ``last_ping_at`` and records what the member ``provides`` (os/arch/runtimes/
    packages/connectors/secrets — NAMES only for secrets). Without a ping row a
    member fails ``assign`` preflight loudly ("member-never-pinged"). Idempotent:
    re-pinging updates the row.
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    try:
        mid = UUID(member_id)
    except (ValueError, AttributeError, TypeError):
        return {"error": "invalid_member_id", "code": 422}

    member = db.query(FleetMember).filter(FleetMember.id == mid).first()
    if member is None:
        return {"error": "member_not_found", "code": 404}
    fleet = db.query(Fleet).filter(Fleet.id == member.fleet_id).first()
    if not _can_manage(db, ctx, fleet):
        return {"error": "forbidden", "code": 403}

    prov = provides if isinstance(provides, dict) else {}
    row = db.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == mid).first()
    if row is None:
        row = FleetMemberLiveness(member_id=mid, provides=prov)
        if reconcile_interval_seconds:
            row.reconcile_interval_seconds = reconcile_interval_seconds
        db.add(row)
    else:
        row.provides = prov
        row.last_ping_at = func.now()
        if reconcile_interval_seconds:
            row.reconcile_interval_seconds = reconcile_interval_seconds
    db.commit()
    return {"ok": True, "member_id": str(mid), "provides_keys": sorted(prov.keys())}


def loopskill_declare_loop(
    db: Session,
    fleet_id: str,
    loop_id: str,
    schedule: str,
    prompt: str,
    *,
    skills: list[dict[str, Any]] | None = None,
    requires: dict[str, Any] | None = None,
    secret_refs: list[dict[str, Any]] | None = None,
    model: str | None = None,
    deliver: str | None = None,
    safety_class: str = "best-effort",
    state_class: str = "stateless",
    concurrency_policy: str = "forbid",
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Declare (or update) a loop's desired-state manifest. Upsert by loop_id.

    This is the write path that populates ``loop_manifests`` — the desired state
    the reconcile engine and golden-bundle composer read. Owner/master only.
    Re-declaring the same loop_id bumps ``manifest_version`` and updates fields.
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    try:
        fid = UUID(fleet_id)
    except (ValueError, AttributeError, TypeError):
        return {"error": "invalid_fleet_id", "code": 422}
    fleet = db.query(Fleet).filter(Fleet.id == fid).first()
    if fleet is None:
        return {"error": "fleet_not_found", "code": 404}
    if not _can_manage(db, ctx, fleet):
        return {"error": "forbidden", "code": 403}

    loop_id = (loop_id or "").strip()
    if not loop_id or len(loop_id) > 128:
        return {"error": "invalid_loop_id", "code": 422}
    if not (schedule or "").strip():
        return {"error": "invalid_schedule", "code": 422}
    if not (prompt or "").strip():
        return {"error": "invalid_prompt", "code": 422}
    if safety_class not in _VALID_SAFETY:
        return {"error": f"safety_class must be one of {_VALID_SAFETY}", "code": 422}
    if state_class not in _VALID_STATE:
        return {"error": f"state_class must be one of {_VALID_STATE}", "code": 422}
    if concurrency_policy not in _VALID_CONCURRENCY:
        return {"error": f"concurrency_policy must be one of {_VALID_CONCURRENCY}", "code": 422}

    owner_user_id = fleet.owner_user_id
    org_id = getattr(fleet, "org_id", None)

    existing = (
        db.query(LoopManifest)
        .filter(
            LoopManifest.loop_id == loop_id,
            LoopManifest.owner_user_id == owner_user_id,
            LoopManifest.org_id == org_id,
        )
        .first()
    )
    fields = dict(
        schedule=schedule,
        prompt=prompt,
        skills=skills or [],
        requires=requires or {},
        secret_refs=secret_refs or [],
        model=model,
        deliver=deliver,
        safety_class=safety_class,
        state_class=state_class,
        concurrency_policy=concurrency_policy,
    )
    if existing is not None:
        for k, v in fields.items():
            setattr(existing, k, v)
        existing.manifest_version = (existing.manifest_version or 1) + 1
        db.commit()
        db.refresh(existing)
        return {
            "ok": True,
            "loop_id": loop_id,
            "manifest_id": str(existing.id),
            "manifest_version": existing.manifest_version,
            "created": False,
        }

    manifest = LoopManifest(
        id=uuid4(),
        loop_id=loop_id,
        owner_user_id=owner_user_id,
        org_id=org_id,
        **fields,
    )
    db.add(manifest)
    db.commit()
    db.refresh(manifest)
    return {
        "ok": True,
        "loop_id": loop_id,
        "manifest_id": str(manifest.id),
        "manifest_version": 1,
        "created": True,
    }


def dispatch_ingest(name: str, db: Session, args: dict[str, Any], ctx: AuthContext) -> Any:
    """Delegated-dispatch hook (matches fleet_write/placement/harvest)."""
    if name == "loopskill_ping":
        return loopskill_ping(
            db,
            args.get("member_id"),
            provides=args.get("provides"),
            reconcile_interval_seconds=args.get("reconcile_interval_seconds"),
            ctx=ctx,
        )
    if name == "loopskill_declare_loop":
        return loopskill_declare_loop(
            db,
            args.get("fleet_id"),
            args.get("loop_id"),
            args.get("schedule"),
            args.get("prompt"),
            skills=args.get("skills"),
            requires=args.get("requires"),
            secret_refs=args.get("secret_refs"),
            model=args.get("model"),
            deliver=args.get("deliver"),
            safety_class=args.get("safety_class", "best-effort"),
            state_class=args.get("state_class", "stateless"),
            concurrency_policy=args.get("concurrency_policy", "forbid"),
            ctx=ctx,
        )
    return _NOT_HANDLED
