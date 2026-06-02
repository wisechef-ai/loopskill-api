"""MCP tool: recipes_feedback.

Send user feedback about recipes.wisechef.ai. Reuses the same
signature/ratelimit/dispatch helpers as POST /api/v1/feedback.

Phase J (loopclose_3005): if the caller's cookbook has a configured
``feedback_repo``, the feedback is dispatched as a GitHub issue to THEIR
repo instead of wisechef-ai/recipes-api.  The default path (no custom
routing) is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app import feedback_ratelimit, github_dispatch
from app.auth_ctx import AuthContext
from app.models import Cookbook, FeedbackSubmission

logger = logging.getLogger(__name__)


def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _resolve_feedback_target(
    db: Session,
    api_key_id: str | None,
    ctx: AuthContext | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the feedback routing target for the caller.

    Returns (repo, mode, encrypted_pat):
      - repo=None  → use the default dispatch_event path (wisechef-ai/recipes-api)
      - repo set   → route to user's repo via dispatch_issue with decrypted PAT
    """
    if ctx is None or ctx.user_id is None:
        return None, None, None

    cb = (
        db.query(Cookbook)
        .filter(
            Cookbook.cookbook_owner == ctx.user_id,
            Cookbook.is_base.is_(False),
            Cookbook.feedback_repo.isnot(None),
        )
        .order_by(Cookbook.created_at.asc())
        .first()
    )
    if cb is None or not cb.feedback_repo:
        return None, None, None

    return cb.feedback_repo, cb.feedback_mode, cb.feedback_pat_enc


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
    ctx: AuthContext | None = None,
) -> dict:
    """Send feedback about recipes.wisechef.ai.

    Use when the user says 'write feedback that...', 'give feedback...',
    'report that...', or expresses frustration with the platform UX,
    search, billing, or docs. Auto-creates a labelled GitHub issue.
    Rate limited per 24h.

    Phase J: Pro/Pro+ users with a configured feedback_repo will have their
    feedback dispatched as issues to their own GitHub repo.
    """
    # Public-scope MCP tool: rate-limited user feedback submission; no private data exposed.
    valid_categories = {"ux", "search", "billing", "docs", "install", "other"}
    if category not in valid_categories:
        return {"ok": False, "error": f"invalid category; must be one of {sorted(valid_categories)}"}

    if not message or len(message) > 4096:
        return {"ok": False, "error": "message must be 1-4096 characters"}

    ctx_obj = context or {}
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
        context=ctx_obj,
        agent_id=agent_id,
        api_key_id=api_key_id,
        signature=sig,
        issue_url="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # ── Phase J: resolve feedback target ────────────────────────────────────
    user_repo, user_mode, user_pat_enc = _resolve_feedback_target(db, api_key_id, ctx)

    gh_url: str = ""
    routed_to_user_repo = False

    if user_repo and user_mode == "pat" and user_pat_enc:
        # Decrypt PAT in-memory — never log plaintext
        try:
            from app.feedback_cred_vault import decrypt_pat

            token = decrypt_pat(user_pat_enc)
            title = f"[{category}] {message[:80]}" + ("…" if len(message) > 80 else "")
            body_md = (
                f"**Category:** {category}\n\n"
                f"**Message:**\n{message}\n\n"
                f"**Signature:** `{sig[:16]}…`\n"
                f"**Submission ID:** {row.id}\n"
            )
            if ctx_obj:
                import json

                body_md += f"\n**Context:**\n```json\n{json.dumps(ctx_obj, indent=2)}\n```\n"

            url = github_dispatch.dispatch_issue(
                user_repo,
                token,
                title=title,
                body=body_md,
                labels=["feedback", category],
            )
            if url:
                gh_url = url
                routed_to_user_repo = True
                logger.info(
                    "feedback: routed to user repo=%s issue_url=%s",
                    user_repo,
                    gh_url,
                )
            else:
                logger.warning(
                    "feedback: user repo dispatch failed for repo=%s — falling back to default",
                    user_repo,
                )
        # Rationale: PAT decryption/dispatch errors must not crash the feedback write
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedback: user-repo dispatch raised: %s — falling back to default", exc)

    # Fall back to default dispatch if user-repo routing failed or not configured
    if not routed_to_user_repo:
        result = github_dispatch.dispatch_event(
            "feedback",
            {
                "id": str(row.id),
                "category": category,
                "message": message,
                "context": ctx_obj,
                "agent_id": agent_id,
                "signature": sig,
            },
        )
        gh_url = "" if not result or result is True else str(result)

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
