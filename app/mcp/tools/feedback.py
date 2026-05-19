"""MCP tool: recipes_feedback.

Send user feedback about recipes.wisechef.ai. Reuses the same
signature/ratelimit/dispatch helpers as POST /api/v1/feedback.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app import feedback_ratelimit, github_dispatch
from app.models import FeedbackSubmission

logger = logging.getLogger(__name__)


def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def recipes_feedback(
    db: Session,
    *,
    category: str,
    message: str,
    context: dict[str, Any] | None = None,
    agent_id: str | None = None,
    force: bool = False,
    confirmation: str | None = None,
    api_key_id: str | None = None,
) -> dict:
    """Send feedback about recipes.wisechef.ai.

    Use when the user says 'write feedback that...', 'give feedback...',
    'report that...', or expresses frustration with the platform UX,
    search, billing, or docs. Auto-creates a labelled GitHub issue.
    Rate limited per 24h.
    """
    # Public-scope MCP tool: rate-limited user feedback submission; no private data exposed.
    valid_categories = {"ux", "search", "billing", "docs", "install", "other"}
    if category not in valid_categories:
        return {"ok": False, "error": f"invalid category; must be one of {sorted(valid_categories)}"}

    if not message or len(message) > 4096:
        return {"ok": False, "error": "message must be 1-4096 characters"}

    ctx = context or {}
    identity = f"api_key:{api_key_id}" if api_key_id else (f"agent:{agent_id}" if agent_id else "unknown")
    sig = _sha256(category, message)

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool="feedback",
        signature=sig,
        force=force,
        confirmation=confirmation,
    )

    if not rl.allowed:
        if rl.deduped:
            return {
                "ok": True,
                "id": "",
                "issue_url": rl.issue_url,
                "deduped": True,
                "last_submissions": [],
                "force_available": False,
            }
        if rl.loop_block:
            return {
                "ok": False,
                "error": "loop_detector_cooldown",
                "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
                "force_available": False,
            }
        return {
            "ok": False,
            "error": "rate_limit_exceeded",
            "force_available": rl.force_available,
            "last_submissions": rl.last_submissions,
        }

    row = FeedbackSubmission(
        category=category,
        message=message,
        context=ctx,
        agent_id=agent_id,
        api_key_id=api_key_id,
        signature=sig,
        issue_url="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    gh_url = (
        github_dispatch.dispatch_event(
            "feedback",
            {
                "id": str(row.id),
                "category": category,
                "message": message,
                "context": ctx,
                "agent_id": agent_id,
                "signature": sig,
            },
        )
        or ""
    )

    if gh_url:
        row.issue_url = gh_url
        db.commit()
        feedback_ratelimit.update_dedup_url(sig, gh_url)

    return {
        "ok": True,
        "id": str(row.id),
        "issue_url": gh_url,
        "deduped": False,
        "last_submissions": [],
        "force_available": False,
    }
