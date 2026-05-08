"""MCP tool: recipes_request_recipe.

Request a new recipe (skill) to be added to the marketplace.
Reuses the same signature/ratelimit/dispatch helpers as
POST /api/v1/recipify-request.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app import github_dispatch, feedback_ratelimit
from app.models import RecipifyRequest

logger = logging.getLogger(__name__)


def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def recipes_request_recipe(
    db: Session,
    *,
    target_name: str,
    why_useful: str,
    suggested_sources: list[str] | None = None,
    agent_id: str | None = None,
    api_key_id: str | None = None,
) -> dict:
    """Request a new recipe (skill).

    Use when the user says 'recipify X', 'please add X to recipes',
    'we need a recipe for X'. Creates a GitHub wishlist issue.
    """
    if not target_name or len(target_name) > 128:
        return {"ok": False, "error": "target_name must be 1-128 characters"}
    if not why_useful or len(why_useful) > 2048:
        return {"ok": False, "error": "why_useful must be 1-2048 characters"}

    sources = suggested_sources or []
    identity = f"api_key:{api_key_id}" if api_key_id else (
        f"agent:{agent_id}" if agent_id else "unknown"
    )
    sig = _sha256(target_name, why_useful)

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool="recipify-request",
        signature=sig,
    )

    if not rl.allowed:
        if rl.deduped:
            return {
                "ok": True, "id": "", "issue_url": rl.issue_url, "deduped": True,
            }
        if rl.loop_block:
            return {
                "ok": False, "error": "loop_detector_cooldown",
                "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
            }
        return {
            "ok": False, "error": "rate_limit_exceeded",
            "force_available": rl.force_available,
        }

    row = RecipifyRequest(
        target_name=target_name,
        why_useful=why_useful,
        suggested_sources=sources,
        agent_id=agent_id,
        api_key_id=api_key_id,
        signature=sig,
        issue_url="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    gh_url = github_dispatch.dispatch_event(
        "recipify-request",
        {
            "id": str(row.id),
            "target_name": target_name,
            "why_useful": why_useful,
            "suggested_sources": sources,
            "agent_id": agent_id,
            "signature": sig,
        },
    ) or ""

    if gh_url:
        row.issue_url = gh_url
        db.commit()
        feedback_ratelimit.update_dedup_url(sig, gh_url)

    return {
        "ok": True,
        "id": str(row.id),
        "issue_url": gh_url,
        "deduped": False,
    }
