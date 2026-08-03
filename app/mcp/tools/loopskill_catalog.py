"""MCP tools for the runnable catalog types — loops + personalities.

loopskill_0622 Phase 8. Lets an agent discover and pull loops/personalities over
MCP, the same way loopskill_search/loopskill_install work for skills. Reuses the
SQLAlchemy primitives directly (no HTTP loopback).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app import authz
from app.auth_ctx import AuthContext
from app.models import Loop, Personality


def loopskill_search_loops(
    db: Session,
    query: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search public loops. Returns {results, total}."""
    # Public-scope MCP tool: public loop registry only; is_public + non-archived filters applied internally, no user-specific data returned.
    q = (
        db.query(Loop)
        .options(joinedload(Loop.versions))
        .filter(Loop.is_public.is_(True), Loop.is_archived.is_(False))
    )
    if category:
        q = q.filter(Loop.category == category)
    if query:
        like = f"%{query}%"
        q = q.filter(or_(Loop.title.ilike(like), Loop.description.ilike(like)))
    rows = q.order_by(Loop.install_count.desc()).limit(min(limit, 200)).all()
    results = [
        {
            "slug": r.slug,
            "title": r.title,
            "description": r.description,
            "category": r.category,
            "tier": r.tier,
            "max_turns": r.max_turns,
            "budget_usd": float(r.budget_usd) if r.budget_usd is not None else None,
            "tool_allowlist": r.tool_allowlist or [],
            "install_count": r.install_count or 0,
            "latest_version": r.versions[0].semver if r.versions else None,
        }
        for r in rows
    ]
    return {"results": results, "total": len(results)}


def loopskill_get_loop(db: Session, slug: str, ctx: AuthContext | None = None) -> dict[str, Any]:
    """Pull a loop's full safety-bounded contract by slug.

    Authz gated: a private (is_public=False) loop's contract (system_prompt,
    verification_script, ...) is only readable by its creator or master —
    gated via authz.can_read_verifier. Unauthorized/nonexistent/archived all
    return the same not-found shape (no existence oracle).
    """
    r = db.query(Loop).options(joinedload(Loop.versions)).filter(Loop.slug == slug).first()
    if r is None or r.is_archived:
        return {"error": "loop not found", "slug": slug, "status": 404}
    if not authz.can_read_verifier(ctx or AuthContext.anonymous(), r, db=db):
        return {"error": "loop not found", "slug": slug, "status": 404}
    return {
        "slug": r.slug,
        "title": r.title,
        "description": r.description,
        "category": r.category,
        "tier": r.tier,
        "readme": r.readme,
        "license": r.license,
        "success_condition": r.success_condition,
        "verification_script": r.verification_script,
        "system_prompt": r.system_prompt,
        "max_turns": r.max_turns,
        "budget_usd": float(r.budget_usd) if r.budget_usd is not None else None,
        "tool_allowlist": r.tool_allowlist or [],
        "stopping_criteria": r.stopping_criteria or {},
        "install_count": r.install_count or 0,
        "latest_version": r.versions[0].semver if r.versions else None,
    }


def loopskill_search_personalities(
    db: Session,
    query: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search public personalities. Returns {results, total}."""
    # Public-scope MCP tool: public personality registry only; is_public + non-archived filters applied internally, no user-specific data returned.
    q = (
        db.query(Personality)
        .options(joinedload(Personality.versions))
        .filter(Personality.is_public.is_(True), Personality.is_archived.is_(False))
    )
    if category:
        q = q.filter(Personality.category == category)
    if query:
        like = f"%{query}%"
        q = q.filter(or_(Personality.title.ilike(like), Personality.description.ilike(like)))
    rows = q.order_by(Personality.install_count.desc()).limit(min(limit, 200)).all()
    results = [
        {
            "slug": r.slug,
            "title": r.title,
            "description": r.description,
            "category": r.category,
            "tier": r.tier,
            "install_count": r.install_count or 0,
            "latest_version": r.versions[0].semver if r.versions else None,
        }
        for r in rows
    ]
    return {"results": results, "total": len(results)}


def loopskill_get_personality(db: Session, slug: str, ctx: AuthContext | None = None) -> dict[str, Any]:
    """Pull a personality's system prompt + config by slug.

    Authz gated: a private (is_public=False) personality's system_prompt +
    config are only readable by its creator or master — gated via
    authz.can_read_personality. Unauthorized/nonexistent/archived all return
    the same not-found shape (no existence oracle).
    """
    r = (
        db.query(Personality)
        .options(joinedload(Personality.versions))
        .filter(Personality.slug == slug)
        .first()
    )
    if r is None or r.is_archived:
        return {"error": "personality not found", "slug": slug, "status": 404}
    if not authz.can_read_personality(ctx or AuthContext.anonymous(), r, db=db):
        return {"error": "personality not found", "slug": slug, "status": 404}
    return {
        "slug": r.slug,
        "title": r.title,
        "description": r.description,
        "category": r.category,
        "tier": r.tier,
        "readme": r.readme,
        "license": r.license,
        "system_prompt": r.system_prompt,
        "config": r.config,
        "install_count": r.install_count or 0,
        "latest_version": r.versions[0].semver if r.versions else None,
    }
