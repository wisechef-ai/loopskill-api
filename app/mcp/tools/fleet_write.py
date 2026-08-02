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
    Bundle,
    BundleCompositeLoop,
    BundleConnector,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    Connector,
    Fleet,
    FleetMember,
    Skill,
)

logger = logging.getLogger(__name__)


def _bulk_declare_junction(
    db: Session,
    bundle_id: UUID,
    items: list[dict[str, Any]],
    *,
    entity_model: Any,
    junction_model: Any,
    junction_fk_name: str,
    extra_field: tuple[str, str] | None = None,
    static_kwargs: dict[str, Any] | None = None,
) -> int:
    """Batch-resolve entities by slug + batch-check existing junction rows,
    then insert any missing junction rows.

    Replaces the per-item N+1 pattern (one Skill/Connector/CompositeLoop/
    Personality lookup by slug + one BundleSkill/BundleConnector/... existence
    check, both issued inside the loop) with one IN-query for the entities and
    one IN-query for existing junction rows — same batched-lookup pattern as
    ``_install_counts_for`` / ``search_skills`` (Issue #19). Used by all four
    ``loopskill_bundle_deploy`` sections (skills, connectors, composite_loops,
    personalities), which shared the identical anti-pattern (qa0208/A-075..078).

    ``extra_field`` is an optional ``(item_dict_key, junction_kwarg_name)`` pair
    for the one per-item extra column (``pinned_version`` for skills,
    ``pinned_semver`` for connectors; composite_loops/personalities have none).
    ``static_kwargs`` are extra constructor kwargs identical for every inserted
    row (e.g. ``BundleSkill.source="declared"``), preserving pre-refactor
    behavior exactly.

    Returns the count of newly-added junction rows.
    """
    slugs = {it.get("slug") for it in items if it.get("slug")}
    if not slugs:
        return 0
    entities_by_slug = {e.slug: e for e in db.query(entity_model).filter(entity_model.slug.in_(slugs)).all()}
    entity_ids = [e.id for e in entities_by_slug.values()]
    fk_col = getattr(junction_model, junction_fk_name)
    existing_ids: set = set()
    if entity_ids:
        existing_ids = {
            getattr(row, junction_fk_name)
            for row in (
                db.query(junction_model)
                .filter(junction_model.bundle_id == bundle_id, fk_col.in_(entity_ids))
                .all()
            )
        }

    added = 0
    for it in items:
        slug = it.get("slug")
        if not slug:
            continue
        entity = entities_by_slug.get(slug)
        if entity is None:
            continue
        if entity.id in existing_ids:
            continue
        kwargs: dict[str, Any] = {"bundle_id": bundle_id, junction_fk_name: entity.id}
        if extra_field is not None:
            src_key, kw_name = extra_field
            kwargs[kw_name] = it.get(src_key)
        if static_kwargs:
            kwargs.update(static_kwargs)
        db.add(junction_model(**kwargs))
        existing_ids.add(entity.id)  # avoid double-insert if the caller passed a dup slug
        added += 1
    return added


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
        added["skills"] = _bulk_declare_junction(
            db,
            bundle.id,
            skills,
            entity_model=Skill,
            junction_model=BundleSkill,
            junction_fk_name="skill_id",
            extra_field=("pinned_version", "pinned_version"),
            static_kwargs={"source": "declared"},
        )

    if connectors:
        added["connectors"] = _bulk_declare_junction(
            db,
            bundle.id,
            connectors,
            entity_model=Connector,
            junction_model=BundleConnector,
            junction_fk_name="connector_id",
            extra_field=("pinned_semver", "pinned_semver"),
        )

    if composite_loops:
        added["composite_loops"] = _bulk_declare_junction(
            db,
            bundle.id,
            composite_loops,
            entity_model=CompositeLoop,
            junction_model=BundleCompositeLoop,
            junction_fk_name="composite_loop_id",
        )

    if personalities:
        from app.models import Personality

        added["personalities"] = _bulk_declare_junction(
            db,
            bundle.id,
            personalities,
            entity_model=Personality,
            junction_model=BundlePersonality,
            junction_fk_name="personality_id",
        )

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

    from app.models import RecipifyRequest, SkillErrorReport

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
