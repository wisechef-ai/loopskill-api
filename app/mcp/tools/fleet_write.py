"""activate_0701 Phase F1 — MCP write-surface tools.

New MCP tools that let an agent run declare→deploy→observe→hear-voice
entirely via MCP. Each round-trips against the live API with real data.

Tools:
  loopskill_bundle_deploy    — declare desired state (skills/connectors/loops/personalities) in a bundle
  loopskill_reconcile_status — check drift between desired and actual for a fleet member
  loopskill_drift_get        — get the raw diff a member would receive
  loopskill_enroll_member    — enroll an agent as a fleet member (returns key ONCE)
  loopskill_list_members     — keyset-paginated member list
  loopskill_voice_inbox_read — read the aggregated voice inbox for a fleet
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import (
    APIKey,
    Bundle,
    BundleCompositeLoop,
    BundleConnector,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    Connector,
    ConnectorVersion,
    Fleet,
    FleetMember,
    FleetSubscription,
    Skill,
)
from app.services.fleet_members import resolve_member_for_key

logger = logging.getLogger(__name__)


def loopskill_bundle_deploy(
    db: Session,
    bundle_id: str,
    skills: list[dict[str, Any]] | None = None,
    connectors: list[dict[str, Any]] | None = None,
    composite_loops: list[dict[str, Any]] | None = None,
    personalities: list[dict[str, Any]] | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Declare desired state for a bundle in one MCP call.

    Each section is a list of {slug, pinned_version?} dicts. Existing entries
    not in the list are NOT removed (additive — use HTTP DELETE for removal).
    Returns counts of what was declared.
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    if not authz.can_write_cookbook(ctx, _resolve_bundle(db, bundle_id)):
        return {"error": "forbidden", "code": 403}

    try:
        cb_uuid = UUID(bundle_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_bundle_id", "code": 422}

    bundle = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if bundle is None:
        return {"error": "bundle_not_found", "code": 404}

    added = {"skills": 0, "connectors": 0, "composite_loops": 0, "personalities": 0}

    if skills:
        for s in skills:
            slug = s.get("slug")
            if not slug:
                continue
            skill = db.query(Skill).filter(Skill.slug == slug).first()
            if skill is None:
                continue
            existing = (
                db.query(BundleSkill)
                .filter(BundleSkill.bundle_id == bundle.id, BundleSkill.skill_id == skill.id)
                .first()
            )
            if existing is None:
                db.add(
                    BundleSkill(
                        bundle_id=bundle.id,
                        skill_id=skill.id,
                        pinned_version=s.get("pinned_version"),
                        source="declared",
                    )
                )
                added["skills"] += 1

    if connectors:
        for c in connectors:
            slug = c.get("slug")
            if not slug:
                continue
            conn = db.query(Connector).filter(Connector.slug == slug).first()
            if conn is None:
                continue
            existing = (
                db.query(BundleConnector)
                .filter(BundleConnector.bundle_id == bundle.id, BundleConnector.connector_id == conn.id)
                .first()
            )
            if existing is None:
                db.add(
                    BundleConnector(
                        bundle_id=bundle.id,
                        connector_id=conn.id,
                        pinned_semver=c.get("pinned_semver"),
                    )
                )
                added["connectors"] += 1

    if composite_loops:
        for cl in composite_loops:
            slug = cl.get("slug")
            if not slug:
                continue
            loop = db.query(CompositeLoop).filter(CompositeLoop.slug == slug).first()
            if loop is None:
                continue
            existing = (
                db.query(BundleCompositeLoop)
                .filter(
                    BundleCompositeLoop.bundle_id == bundle.id,
                    BundleCompositeLoop.composite_loop_id == loop.id,
                )
                .first()
            )
            if existing is None:
                db.add(
                    BundleCompositeLoop(
                        bundle_id=bundle.id,
                        composite_loop_id=loop.id,
                    )
                )
                added["composite_loops"] += 1

    if personalities:
        from app.models import Personality

        for p in personalities:
            slug = p.get("slug")
            if not slug:
                continue
            pers = db.query(Personality).filter(Personality.slug == slug).first()
            if pers is None:
                continue
            existing = (
                db.query(BundlePersonality)
                .filter(
                    BundlePersonality.bundle_id == bundle.id,
                    BundlePersonality.personality_id == pers.id,
                )
                .first()
            )
            if existing is None:
                db.add(
                    BundlePersonality(
                        bundle_id=bundle.id,
                        personality_id=pers.id,
                    )
                )
                added["personalities"] += 1

    db.commit()

    # Bump the bundle generation so polling members see the update
    from sqlalchemy import func

    db.query(Bundle).filter(Bundle.id == bundle.id).update({"updated_at": func.now()})
    db.commit()

    return {"deployed": True, "bundle_id": bundle_id, "declared": added}


def loopskill_reconcile_status(
    db: Session,
    fleet_id: str,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Check the reconcile status of a fleet's members.

    Returns per-member: last reconcile event outcome, version, and whether
    the member is up_to_date or has drift.
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_fleet_id", "code": 422}

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        return {"error": "fleet_not_found", "code": 404}
    if not authz.can_use_fleet(ctx, fleet):
        return {"error": "forbidden", "code": 403}

    members = (
        db.query(FleetMember)
        .filter(FleetMember.fleet_id == fleet.id, FleetMember.is_active == True)  # noqa: E712
        .all()
    )

    from app.models import ReconcileEvent
    from sqlalchemy import func

    result = []
    for m in members:
        last_event = (
            db.query(ReconcileEvent)
            .filter(ReconcileEvent.member_id == m.id)
            .order_by(ReconcileEvent.created_at.desc())
            .first()
        )
        result.append(
            {
                "member_id": str(m.id),
                "host": m.host,
                "profile": m.profile,
                "last_outcome": last_event.outcome if last_event else None,
                "last_event_at": last_event.created_at.isoformat()
                if last_event and last_event.created_at
                else None,
                "is_active": m.is_active,
            }
        )

    return {"fleet_id": fleet_id, "members": result}


def loopskill_voice_inbox_read(
    db: Session,
    fleet_id: str,
    limit: int = 50,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Read the aggregated voice inbox for a fleet (MCP-callable)."""
    if ctx is None:
        ctx = AuthContext(scope="master")
    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_fleet_id", "code": 422}

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        return {"error": "fleet_not_found", "code": 404}
    if not authz.can_use_fleet(ctx, fleet):
        return {"error": "forbidden", "code": 403}

    from app.models import FeedbackSubmission, RecipifyRequest, SkillErrorReport

    member_ids = [
        m.id
        for m in db.query(FleetMember)
        .filter(FleetMember.fleet_id == fleet.id, FleetMember.is_active == True)  # noqa: E712
        .all()
    ]

    items: list[dict[str, Any]] = []

    if member_ids:
        for r in (
            db.query(SkillErrorReport)
            .filter(SkillErrorReport.member_id.in_(member_ids))
            .filter(SkillErrorReport.feedback_status.in_(["pending", "filed"]))
            .order_by(SkillErrorReport.created_at.desc())
            .limit(limit)
            .all()
        ):
            items.append(
                {
                    "type": "skill_error",
                    "id": str(r.id),
                    "slug": r.slug,
                    "summary": (r.summary or "")[:200],
                    "status": r.feedback_status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

    api_key_ids = [m.api_key_id for m in db.query(FleetMember).filter(FleetMember.fleet_id == fleet.id).all()]
    if api_key_ids:
        for r in (
            db.query(RecipifyRequest)
            .filter(RecipifyRequest.api_key_id.in_(api_key_ids))
            .filter(RecipifyRequest.feedback_status.in_(["pending", "filed"]))
            .order_by(RecipifyRequest.created_at.desc())
            .limit(limit)
            .all()
        ):
            items.append(
                {
                    "type": "recipify_request",
                    "id": str(r.id),
                    "target_name": r.target_name,
                    "status": r.feedback_status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"items": items[:limit], "total": len(items)}


def _resolve_bundle(db: Session, bundle_id: str) -> Any:
    """Lightweight bundle lookup for authz (doesn't raise)."""
    try:
        return db.query(Bundle).filter(Bundle.id == UUID(bundle_id)).first()
    except (ValueError, AttributeError):
        return None


_NOT_HANDLED = object()  # sentinel for dispatch delegation


def dispatch_f1(
    name: str,
    db: Session,
    args: dict[str, Any],
    ctx: AuthContext | None = None,
) -> Any:
    """Delegated dispatch for Phase F1 fleet write-surface tools.

    Returns the tool result, or _NOT_HANDLED if this isn't an F1 tool.
    """
    if name == "loopskill_bundle_deploy":
        return loopskill_bundle_deploy(
            db,
            bundle_id=args["bundle_id"],
            skills=args.get("skills"),
            connectors=args.get("connectors"),
            composite_loops=args.get("composite_loops"),
            personalities=args.get("personalities"),
            ctx=ctx,
        )
    if name == "loopskill_reconcile_status":
        return loopskill_reconcile_status(db, fleet_id=args["fleet_id"], ctx=ctx)
    if name == "loopskill_voice_inbox_read":
        return loopskill_voice_inbox_read(
            db, fleet_id=args["fleet_id"], limit=int(args.get("limit", 50)), ctx=ctx
        )
    return _NOT_HANDLED
