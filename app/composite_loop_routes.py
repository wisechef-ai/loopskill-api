"""Composite loop registry routes — the NEW composite-loop surface.

loopskill_activate_0701 Phase A2. A *composite loop* is a deployable autonomous
work unit: automation(heartbeat/cron) + skills + sub-agents(maker≠checker) +
connectors + verifier(gate) + state_seed.

NEW SURFACE (council §6 binding conditions):
  * Separate table (composite_loops), separate routes (/api/composite-loops).
  * NEVER reuses /api/loops, /api/verifiers, or old MCP loop tool names.
  * /api/loops payload stays byte-identical (the old surface is untouched).

Routes:
  GET  /api/composite-loops            — browse public composite loops (keyset-paginated)
  GET  /api/composite-loops/{slug}     — composite loop detail (public)
  POST /api/composite-loops            — publish a composite loop (auth required)
  POST /api/composite-loops/{slug}/versions — publish a version (auth required)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import CompositeLoop, CompositeLoopVersion
from app.schemas import (
    CompositeLoopDetailOut,
    CompositeLoopOut,
    CompositeLoopPublishIn,
    CompositeLoopVersionIn,
    CompositeLoopVersionOut,
)
from app.services.composite_loop_validation import (
    CompositeLoopValidationError,
    validate_composite_loop_manifest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _composite_loop_to_out(cl: CompositeLoop) -> CompositeLoopOut:
    return CompositeLoopOut(
        id=cl.id,
        slug=cl.slug,
        title=cl.title,
        description=cl.description,
        tier=cl.tier,
        is_public=cl.is_public,
        schedule=cl.schedule,
        verifier_slug=cl.verifier_slug,
        residency=cl.residency,
        install_count=cl.install_count or 0,
        latest_version=cl.versions[0].semver if cl.versions else None,
        created_at=cl.created_at or datetime.now(UTC),
        updated_at=cl.updated_at or datetime.now(UTC),
    )


def list_composite_loops(
    q: str | None = Query(None, description="keyword search over title/description"),
    limit: int = Query(100, le=200),
    db: Session = Depends(get_db),
) -> list[CompositeLoopOut]:
    """Browse public, non-archived composite loops (keyset-paginated)."""
    query = (
        db.query(CompositeLoop)
        .options(joinedload(CompositeLoop.versions))
        .filter(CompositeLoop.is_public.is_(True), CompositeLoop.is_archived.is_(False))
    )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(CompositeLoop.title.ilike(like), CompositeLoop.description.ilike(like)))
    rows = query.order_by(CompositeLoop.install_count.desc()).limit(limit).all()
    return [_composite_loop_to_out(r) for r in rows]


def get_composite_loop(slug: str, db: Session = Depends(get_db)) -> CompositeLoopDetailOut:
    """Composite loop detail including the full composition."""
    cl = (
        db.query(CompositeLoop)
        .options(joinedload(CompositeLoop.versions))
        .filter(CompositeLoop.slug == slug)
        .first()
    )
    if cl is None or cl.is_archived:
        raise HTTPException(status_code=404, detail="composite loop not found")
    base = _composite_loop_to_out(cl).model_dump()
    base.update(
        skills=cl.skills or [],
        connectors=cl.connectors or [],
        subagents_config=cl.subagents_config or {},
        state_seed=cl.state_seed or {},
        budget_usd=float(cl.budget_usd) if cl.budget_usd is not None else None,
        prompt=cl.prompt,
        versions=[
            {
                "id": v.id,
                "semver": v.semver,
                "changelog": v.changelog,
                "created_at": v.created_at or datetime.now(UTC),
            }
            for v in cl.versions
        ],
        agent_instructions=_composite_loop_agent_instructions(cl),
    )
    return CompositeLoopDetailOut(**base)


def _composite_loop_agent_instructions(cl: CompositeLoop) -> str:
    """One-shot install instructions for a REMOTE agent reading this JSON.

    ah0723 rank-1: the deploy API (POST /api/composite-loops/{slug}/deploy)
    already requires a logged-in human's fleet_id + member_id (portal-only
    flow, see composite_loop_deploy_routes.py) — a remote/anonymous agent
    can't self-serve a POST with args it doesn't have. This mirrors the
    fetch-and-report contract used by agent_instructions elsewhere
    (skill_routes.py FETCH_ORIGIN, verifier_routes.py post-run) instead of
    inventing a new pattern: tell the calling agent exactly what to relay
    to its human operator to close the loop.
    """
    return (
        f"To run '{cl.slug}' on your own fleet: have your human operator open "
        f"https://app.loopskill.io/loops/view?slug={cl.slug} while signed in, "
        "pick a fleet + agent, and click Deploy — or call "
        f"POST /api/composite-loops/{cl.slug}/deploy with a signed-in session "
        "cookie and JSON body {fleet_id, member_id} (both from GET /api/fleets "
        "and GET /api/fleets/{fleet_id}/members). The target agent applies it "
        "on its next sync tick (~30 min) as a local cron running the schedule "
        f"'{cl.schedule}'. No manual copy-paste of skills/config required — "
        "the deploy call materializes the full composition server-side."
    )


def publish_composite_loop(
    payload: CompositeLoopPublishIn,
    request: Request,
    db: Session = Depends(get_db),
) -> CompositeLoopDetailOut:
    """Publish a composite loop. Auth required; the composition is validated server-side."""
    ctx = getattr(request.state, "auth_ctx", None)
    scope = getattr(ctx, "scope", None)
    if ctx is None or scope in (None, "anonymous"):
        raise HTTPException(status_code=401, detail="authentication required to publish")
    if scope not in ("user", "master"):
        raise HTTPException(
            status_code=403,
            detail=f"scope {scope!r} may not publish composite loops",
        )

    try:
        residency = validate_composite_loop_manifest(
            db,
            verifier_slug=payload.verifier_slug,
            schedule=payload.schedule,
            subagents_config=payload.subagents_config,
            budget_usd=payload.budget_usd,
            skills=payload.skills,
            connectors=payload.connectors,
        )
    except CompositeLoopValidationError as exc:
        raise HTTPException(status_code=422, detail=f"composite loop invalid: {exc}")

    if db.query(CompositeLoop).filter(CompositeLoop.slug == payload.slug).first() is not None:
        raise HTTPException(status_code=409, detail=f"slug {payload.slug!r} exists")

    cl = CompositeLoop(
        id=uuid4(),
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        tier=payload.tier,
        is_public=payload.is_public,
        schedule=payload.schedule,
        skills=payload.skills or [],
        connectors=payload.connectors or [],
        subagents_config=payload.subagents_config or {},
        verifier_slug=payload.verifier_slug,
        state_seed=payload.state_seed or {},
        budget_usd=payload.budget_usd,
        prompt=payload.prompt,
        residency=residency,
        created_at=datetime.now(UTC),
    )
    db.add(cl)
    db.commit()
    db.refresh(cl)
    logger.info("composite loop published: %s", cl.slug)
    return get_composite_loop(cl.slug, db)


def publish_composite_loop_version(
    slug: str,
    payload: CompositeLoopVersionIn,
    request: Request,
    db: Session = Depends(get_db),
) -> CompositeLoopVersionOut:
    """Publish a frozen version of a composite loop. Auth required.

    Version publish bumps the generation of every declaring bundle so the
    304 fast-path breaks and polling agents see the new version.
    """
    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None or getattr(ctx, "scope", None) not in ("user", "master"):
        raise HTTPException(status_code=401, detail="authentication required to publish a version")

    cl = db.query(CompositeLoop).filter(CompositeLoop.slug == slug).first()
    if cl is None or cl.is_archived:
        raise HTTPException(status_code=404, detail="composite loop not found")

    existing = (
        db.query(CompositeLoopVersion)
        .filter(
            CompositeLoopVersion.composite_loop_id == cl.id, CompositeLoopVersion.semver == payload.semver
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"version {payload.semver!r} exists")

    manifest = {
        "slug": cl.slug,
        "title": cl.title,
        "schedule": cl.schedule,
        "skills": cl.skills or [],
        "connectors": cl.connectors or [],
        "subagents_config": cl.subagents_config or {},
        "verifier_slug": cl.verifier_slug,
        "state_seed": cl.state_seed or {},
        "budget_usd": float(cl.budget_usd) if cl.budget_usd is not None else None,
        "prompt": cl.prompt,
        "residency": cl.residency,
    }

    version = CompositeLoopVersion(
        composite_loop_id=cl.id,
        semver=payload.semver,
        manifest=manifest,
        changelog=payload.changelog,
    )
    db.add(version)

    # Bump declaring bundles so the 304 fast-path breaks.
    from app.services.composite_loop_validation import bump_declaring_bundles_for_cl

    bump_declaring_bundles_for_cl(db, cl.id)

    db.commit()
    db.refresh(version)
    logger.info("composite loop version published: %s@%s", cl.slug, payload.semver)
    return CompositeLoopVersionOut(
        id=version.id,
        semver=version.semver,
        changelog=version.changelog,
        created_at=version.created_at or datetime.now(UTC),
    )


# ── route registration ──────────────────────────────────────────────────────

router.add_api_route(
    "/api/composite-loops",
    list_composite_loops,
    methods=["GET"],
    response_model=list[CompositeLoopOut],
)
router.add_api_route(
    "/api/composite-loops/{slug}",
    get_composite_loop,
    methods=["GET"],
    response_model=CompositeLoopDetailOut,
)
router.add_api_route(
    "/api/composite-loops",
    publish_composite_loop,
    methods=["POST"],
    response_model=CompositeLoopDetailOut,
    status_code=201,
)
router.add_api_route(
    "/api/composite-loops/{slug}/versions",
    publish_composite_loop_version,
    methods=["POST"],
    response_model=CompositeLoopVersionOut,
    status_code=201,
)
