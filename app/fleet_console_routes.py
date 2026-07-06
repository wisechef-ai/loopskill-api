"""feat/fleet-console-state — the fleet console read surface.

Answers the operational questions a fleet owner managing N agents needs:

  GET /api/fleets/{fleet_id}/members/{member_id}/state
      One agent's reality: installed skills (from its latest lockfile
      snapshot), drift vs the declared bundle(s), and EXTRAS — skills
      installed on the agent but declared nowhere (harvest candidates:
      "I built something new on Astrovita, promote it to a bundle").

  GET /api/fleets/{fleet_id}/inventory
      The whole fleet as one matrix: per member — installed count, drift
      counts, extras, freshness. One query per table, no N+1 (D9).

Drift semantics (computed at READ time; the bundle stays the single source
of truth, nothing derived is stored):
  in_sync   — declared and installed
  missing   — declared but not installed  (deploy pending / failed)
  extra     — installed but not declared  (harvest candidate)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import authz
from app.database import get_db
from app.fleet_routes import resolve_fleet_ctx
from app.models import (
    Bundle,
    BundleSkill,
    Fleet,
    FleetMember,
    FleetSubscription,
    MemberLockfileSnapshot,
    Skill,
)

router = APIRouter(prefix="/api/fleets", tags=["fleet-console"])


def _resolve_fleet_or_404(db: Session, ctx, fleet_id: str) -> Fleet:
    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="fleet_not_found")
    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None or not authz.can_use_fleet(ctx, fleet):
        raise HTTPException(status_code=404, detail="fleet_not_found")
    return fleet


def _declared_skills_for_fleet(db: Session, fleet: Fleet) -> dict[str, dict[str, Any]]:
    """{slug: {bundle_id, bundle_name, pinned_version}} across ALL subscribed bundles."""
    subs = db.query(FleetSubscription).filter(FleetSubscription.fleet_id == fleet.id).all()
    bundle_ids = [s.bundle_id for s in subs]
    if not bundle_ids:
        return {}
    rows = (
        db.query(BundleSkill, Skill, Bundle)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .join(Bundle, Bundle.id == BundleSkill.bundle_id)
        .filter(BundleSkill.bundle_id.in_(bundle_ids))
        .all()
    )
    declared: dict[str, dict[str, Any]] = {}
    for bs, skill, bundle in rows:
        declared[skill.slug] = {
            "bundle_id": str(bundle.id),
            "bundle_name": bundle.name,
            "pinned_version": bs.pinned_version,
        }
    return declared


def _diff_member(
    snapshot: MemberLockfileSnapshot | None,
    declared: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compute installed/missing/extra for one member snapshot."""
    installed_list = list(snapshot.skills or []) if snapshot is not None else []
    installed_by_slug = {s.get("slug"): s for s in installed_list if s.get("slug")}

    in_sync, extras = [], []
    for slug, inst in installed_by_slug.items():
        entry = {
            "slug": slug,
            "installed_version": inst.get("pinned_version"),
            "checksum_sha256": inst.get("checksum_sha256"),
        }
        if slug in declared:
            entry["bundle_name"] = declared[slug]["bundle_name"]
            entry["bundle_id"] = declared[slug]["bundle_id"]
            in_sync.append(entry)
        else:
            extras.append(entry)

    missing = [{"slug": slug, **meta} for slug, meta in declared.items() if slug not in installed_by_slug]
    return {
        "installed_count": len(installed_list),
        "in_sync": sorted(in_sync, key=lambda x: x["slug"]),
        "missing": sorted(missing, key=lambda x: x["slug"]),
        "extras": sorted(extras, key=lambda x: x["slug"]),
        "reported_at": snapshot.reported_at.isoformat() if snapshot is not None else None,
        "has_snapshot": snapshot is not None,
    }


@router.get("/{fleet_id}/members/{member_id}/state")
def get_member_state(
    fleet_id: str,
    member_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """One agent's reality: installed skills + drift vs declared + extras."""
    ctx = resolve_fleet_ctx(request, db)
    fleet = _resolve_fleet_or_404(db, ctx, fleet_id)

    try:
        member_uuid = UUID(member_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="member_not_found")
    member = (
        db.query(FleetMember).filter(FleetMember.id == member_uuid, FleetMember.fleet_id == fleet.id).first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="member_not_found")

    snapshot = db.query(MemberLockfileSnapshot).filter(MemberLockfileSnapshot.member_id == member.id).first()
    declared = _declared_skills_for_fleet(db, fleet)
    diff = _diff_member(snapshot, declared)
    return {
        "member_id": str(member.id),
        "host": member.host,
        "profile": member.profile,
        "is_active": bool(member.is_active),
        "declared_count": len(declared),
        **diff,
    }


@router.get("/{fleet_id}/inventory")
def get_fleet_inventory(
    fleet_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The whole fleet as one matrix — per-member drift summary + extras.

    One query per table (members, snapshots, declared), no N+1. At 100
    agents this is 100 snapshot rows joined in memory — cheap by design.
    """
    ctx = resolve_fleet_ctx(request, db)
    fleet = _resolve_fleet_or_404(db, ctx, fleet_id)

    members = (
        db.query(FleetMember)
        .filter(FleetMember.fleet_id == fleet.id, FleetMember.is_active == True)  # noqa: E712
        .all()
    )
    snapshots = {
        s.member_id: s
        for s in db.query(MemberLockfileSnapshot).filter(MemberLockfileSnapshot.fleet_id == fleet.id).all()
    }
    declared = _declared_skills_for_fleet(db, fleet)

    rows = []
    for m in members:
        diff = _diff_member(snapshots.get(m.id), declared)
        rows.append(
            {
                "member_id": str(m.id),
                "host": m.host,
                "profile": m.profile,
                "installed_count": diff["installed_count"],
                "in_sync_count": len(diff["in_sync"]),
                "missing_count": len(diff["missing"]),
                "extras_count": len(diff["extras"]),
                "extras": diff["extras"],
                "reported_at": diff["reported_at"],
                "has_snapshot": diff["has_snapshot"],
            }
        )

    return {
        "fleet_id": str(fleet.id),
        "fleet_name": fleet.name,
        "declared_count": len(declared),
        "declared": sorted(declared.keys()),
        "members": sorted(rows, key=lambda r: r["host"]),
    }
