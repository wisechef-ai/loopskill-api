"""MCP tools for the composite loop catalog — activate_0701 Phase A2.

NEW MCP names (council §6 binding conditions):
  loopskill_publish_composite_loop
  loopskill_get_composite_loop
  loopskill_search_composite_loops

These are NEW surfaces — they do NOT reuse the old loop tool names
(loopskill_search_loops / loopskill_get_loop).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app import authz
from app.auth_ctx import AuthContext
from app.models import CompositeLoop
from app.services.composite_loop_validation import (
    CompositeLoopValidationError,
    validate_composite_loop_manifest,
)

# Sentinel so the dispatcher can distinguish "not my tool" from a None result.
_NOT_HANDLED = object()


def loopskill_search_composite_loops(
    db: Session,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search public composite loops. Returns {results, total}."""
    # Public-scope MCP tool: public composite loop registry only; is_public + non-archived filters applied internally, no user-specific data returned.
    q = (
        db.query(CompositeLoop)
        .options(joinedload(CompositeLoop.versions))
        .filter(CompositeLoop.is_public.is_(True), CompositeLoop.is_archived.is_(False))
    )
    if query:
        like = f"%{query}%"
        q = q.filter(or_(CompositeLoop.title.ilike(like), CompositeLoop.description.ilike(like)))
    rows = q.order_by(CompositeLoop.install_count.desc()).limit(min(limit, 200)).all()
    results = [
        {
            "slug": r.slug,
            "title": r.title,
            "description": r.description,
            "tier": r.tier,
            "schedule": r.schedule,
            "verifier_slug": r.verifier_slug,
            "residency": r.residency,
            "install_count": r.install_count or 0,
            "latest_version": r.versions[0].semver if r.versions else None,
        }
        for r in rows
    ]
    return {"results": results, "total": len(results)}


def loopskill_get_composite_loop(db: Session, slug: str, ctx: AuthContext | None = None) -> dict[str, Any]:
    """Pull a composite loop's full composition by slug.

    Authz gated: a private (is_public=False) composite loop's composition
    (skills/connectors/subagents_config/driving prompt — connectors can
    carry credentials) is only readable by its creator or master — gated
    via authz.can_read_composite_loop. Unauthorized/nonexistent/archived
    all return the same not-found shape (no existence oracle).
    """
    r = (
        db.query(CompositeLoop)
        .options(joinedload(CompositeLoop.versions))
        .filter(CompositeLoop.slug == slug)
        .first()
    )
    if r is None or r.is_archived:
        return {"error": "composite loop not found", "slug": slug, "status": 404}
    if not authz.can_read_composite_loop(ctx or AuthContext.anonymous(), r, db=db):
        return {"error": "composite loop not found", "slug": slug, "status": 404}
    return {
        "slug": r.slug,
        "title": r.title,
        "description": r.description,
        "tier": r.tier,
        "schedule": r.schedule,
        "skills": r.skills or [],
        "connectors": r.connectors or [],
        "subagents_config": r.subagents_config or {},
        "verifier_slug": r.verifier_slug,
        "state_seed": r.state_seed or {},
        "budget_usd": float(r.budget_usd) if r.budget_usd is not None else None,
        "prompt": r.prompt,
        "residency": r.residency,
        "install_count": r.install_count or 0,
        "latest_version": r.versions[0].semver if r.versions else None,
    }


def loopskill_publish_composite_loop(
    db: Session,
    *,
    ctx: AuthContext | None = None,
    slug: str,
    title: str,
    schedule: str,
    verifier_slug: str,
    prompt: str,
    description: str | None = None,
    tier: str | None = "free",
    is_public: bool = True,
    skills: list[dict[str, Any]] | None = None,
    connectors: list[dict[str, Any]] | None = None,
    subagents_config: dict[str, Any] | None = None,
    state_seed: dict[str, Any] | None = None,
    budget_usd: float | None = None,
) -> dict[str, Any]:
    """Publish a composite loop via MCP. Validates the full manifest server-side.

    Auth gated: mirrors POST /api/composite-loops (app/composite_loop_routes.py)
    — requires an authenticated ctx with scope 'user' or 'master'. Previously
    this tool carried a comment claiming a ctx check happened in _dispatch,
    but _dispatch never passed ctx to this tool at all, so ANY anonymous MCP
    caller could publish a new catalog entry.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    if ctx is None or ctx.scope in (None, "anonymous"):
        return {"error": "auth_required", "status": 401}
    if ctx.scope not in ("user", "master"):
        return {"error": "forbidden", "status": 403}

    if db.query(CompositeLoop).filter(CompositeLoop.slug == slug).first() is not None:
        return {"error": "slug exists", "slug": slug, "status": 409}

    try:
        residency = validate_composite_loop_manifest(
            db,
            verifier_slug=verifier_slug,
            schedule=schedule,
            subagents_config=subagents_config or {},
            budget_usd=budget_usd,
            skills=skills,
            connectors=connectors,
        )
    except CompositeLoopValidationError as exc:
        return {"error": str(exc), "status": 422}

    cl = CompositeLoop(
        id=uuid4(),
        slug=slug,
        title=title,
        description=description,
        tier=tier,
        is_public=is_public,
        schedule=schedule,
        skills=skills or [],
        connectors=connectors or [],
        subagents_config=subagents_config or {},
        verifier_slug=verifier_slug,
        state_seed=state_seed or {},
        budget_usd=budget_usd,
        prompt=prompt,
        residency=residency,
        created_at=datetime.now(UTC),
    )
    db.add(cl)
    db.commit()
    db.refresh(cl)
    return loopskill_get_composite_loop(db, cl.slug)


def dispatch_composite_loop(
    name: str, db: Session, args: dict[str, Any], ctx: AuthContext | None = None
) -> Any:
    """Dispatch the three Phase A2 composite-loop MCP tools.

    Returns _NOT_HANDLED if the tool name is not a composite-loop tool.
    Extracted to keep server._dispatch under the 600-line module cap.
    """
    if name == "loopskill_publish_composite_loop":
        return loopskill_publish_composite_loop(
            db,
            ctx=ctx,
            slug=args["slug"],
            title=args["title"],
            schedule=args["schedule"],
            verifier_slug=args["verifier_slug"],
            prompt=args["prompt"],
            description=args.get("description"),
            tier=args.get("tier", "free"),
            is_public=args.get("is_public", True),
            skills=args.get("skills"),
            connectors=args.get("connectors"),
            subagents_config=args.get("subagents_config"),
            state_seed=args.get("state_seed"),
            budget_usd=args.get("budget_usd"),
        )
    if name == "loopskill_get_composite_loop":
        return loopskill_get_composite_loop(db, slug=args["slug"], ctx=ctx)
    if name == "loopskill_search_composite_loops":
        return loopskill_search_composite_loops(
            db,
            query=args.get("query"),
            limit=int(args.get("limit", 50)),
        )
    return _NOT_HANDLED
