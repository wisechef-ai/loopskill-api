"""feat/member-loop-apply — GET /api/my/loop-assignments (the member pull surface).

fleetos_1607 Phase I shipped the WRITE path (loopskill_ping + declare_loop) and
Phase A shipped placements — but no member-facing READ surface existed: a fleet
member had no way to ask "which loops am I supposed to be running?". The
placement chain ended at the server's edge, so a declared + assigned loop never
became a local cron on any agent. This module closes the read half of the last
mile; the client half (app/loop_apply.py) translates the response into local
Hermes cron jobs.

Auth contract (mirrors sync_report_routes): the x-api-key MUST resolve to an
active FleetMember. Master keys and plain user keys get 403 — this surface is
the member's own view, not a management console (managers read placements via
the MCP placement tools).

Response shape:
  {
    "member_id": "...",
    "fleet_id": "...",
    "count": N,
    "assignments": [
      {
        "loop_key": "...",
        "placement_id": "...",
        "epoch": 3,
        "status": "assigned" | "active",
        "manifest": {<canonical LoopManifest transport dict>} | null
      }, ...
    ]
  }

``manifest`` is null when a placement exists but no LoopManifest row matches its
loop_key in the fleet owner's scope (declared-out-of-band placements) — the
client MUST skip such rows (nothing to schedule) rather than fabricate a job.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Fleet, LoopManifest, LoopPlacement
from app.services.fleet_artifacts import manifest_to_transport
from app.services.fleet_members import resolve_member_for_key

router = APIRouter(prefix="/api", tags=["loop-assignments"])

# Placement statuses a member should actively schedule. `draining` is excluded:
# a draining member must STOP running the loop (that is what draining means);
# `removed` rows are history.
_SCHEDULABLE_STATUSES = ("assigned", "active")


@router.get("/my/loop-assignments")
def my_loop_assignments(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the calling member's schedulable loop assignments + manifests.

    Auth: x-api-key MUST resolve to an active FleetMember (403 otherwise,
    401 anonymous) — same contract as POST /api/sync-report.
    """
    api_key_id = getattr(request.state, "api_key_id", None)
    auth_ctx = getattr(request.state, "auth_ctx", None)

    if auth_ctx is None and api_key_id is None:
        raise HTTPException(status_code=401, detail="member_key_required")

    member = resolve_member_for_key(db, api_key_id)
    if member is None:
        raise HTTPException(status_code=403, detail="not_a_member_key")

    fleet = db.query(Fleet).filter(Fleet.id == member.fleet_id).first()
    if fleet is None:
        # Member row without a fleet is a data error; fail honestly.
        raise HTTPException(status_code=404, detail="fleet_not_found")

    placements = (
        db.query(LoopPlacement)
        .filter(
            LoopPlacement.member_id == member.id,
            LoopPlacement.status.in_(_SCHEDULABLE_STATUSES),
        )
        .order_by(LoopPlacement.loop_key)
        .all()
    )

    # Resolve each placement's manifest within the fleet's declaration scope.
    # loop_key is a string identity (not an FK) — see LoopPlacement docstring.
    #
    # fix/loop-assignment-scope-match: declare_loop (fleet_ingest) stamps BOTH
    # owner_user_id AND org_id from the fleet onto every manifest, so the read
    # side must match BOTH — including org_id IS NULL for personal fleets. The
    # original org-XOR-owner filter returned manifest=null for every loop of an
    # org-scoped fleet (found live wiring Tori as the first member).
    assignments: list[dict[str, Any]] = []
    for p in placements:
        mq = db.query(LoopManifest).filter(
            LoopManifest.loop_id == p.loop_key,
            LoopManifest.enabled == True,  # noqa: E712
            LoopManifest.owner_user_id == fleet.owner_user_id,
        )
        if fleet.org_id is not None:
            mq = mq.filter(LoopManifest.org_id == fleet.org_id)
        else:
            mq = mq.filter(LoopManifest.org_id.is_(None))
        manifest = mq.first()
        assignments.append(
            {
                "loop_key": p.loop_key,
                "placement_id": str(p.id),
                "epoch": p.placement_epoch,
                "status": p.status,
                "manifest": manifest_to_transport(manifest) if manifest is not None else None,
            }
        )

    return {
        "member_id": str(member.id),
        "fleet_id": str(member.fleet_id),
        "count": len(assignments),
        "assignments": assignments,
    }
