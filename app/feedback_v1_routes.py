"""Stream 1 — POST /api/v1/recipify-request and POST /api/v1/feedback.

Both endpoints:
  - Require x-api-key via APIKeyMiddleware (already in the stack).
  - Compute a sha256 signature for deduplication.
  - Apply the multi-window rate limiter from app.feedback_ratelimit.
  - Persist to the DB (durable write first).
  - Fire a GitHub repository_dispatch event (best-effort; failure != 500).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import feedback_ratelimit, github_dispatch
from app.database import get_db
from app.models import FeedbackSubmission, RecipifyRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["feedback-v1"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_identity(request: Request, agent_id: str | None) -> str:
    """Resolve caller identity for rate-limiting.

    Priority: api_key_id from middleware state > agent_id > peer IP.
    """
    state = getattr(request, "state", None)
    if state:
        api_key_id = getattr(state, "api_key_id", None)
        if api_key_id:
            return str(api_key_id)
    if agent_id:
        return f"agent:{agent_id}"
    # Fall back to peer IP
    forwarded = getattr(getattr(request, "headers", None), "get", lambda k, d="": d)("x-forwarded-for", "")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    client = getattr(request, "client", None)
    if client:
        return f"ip:{client.host}"
    return "ip:unknown"


def _get_api_key_id(request: Request) -> UUID | None:
    state = getattr(request, "state", None)
    if state:
        v = getattr(state, "api_key_id", None)
        if v:
            return UUID(str(v)) if not isinstance(v, UUID) else v
    return None


def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ── Schemas ───────────────────────────────────────────────────────────────────


class RecipifyRequestIn(BaseModel):
    target_name: str = Field(min_length=1, max_length=128)
    why_useful: str = Field(min_length=1, max_length=2048)
    suggested_sources: list[str] = Field(default_factory=list, max_length=10)
    agent_id: str | None = Field(default=None, max_length=128)


class RecipifyRequestOut(BaseModel):
    ok: bool
    id: str
    issue_url: str
    deduped: bool = False
    retry_at: datetime | None = None


class FeedbackIn(BaseModel):
    category: Literal["ux", "search", "billing", "docs", "install", "other"]
    message: str = Field(min_length=1, max_length=4096)
    context: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = Field(default=None, max_length=128)
    force: bool = False
    confirmation: str | None = Field(default=None, max_length=128)


class FeedbackOut(BaseModel):
    ok: bool
    id: str
    issue_url: str
    deduped: bool = False
    last_submissions: list[dict] = Field(default_factory=list)
    retry_at: datetime | None = None
    force_available: bool = False


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/recipify-request",
    response_model=RecipifyRequestOut,
    status_code=status.HTTP_201_CREATED,
)
def post_recipify_request(
    payload: RecipifyRequestIn,
    request: Request,
    db: Session = Depends(get_db),
) -> RecipifyRequestOut:
    """Submit a recipify (skill creation) request."""
    identity = _get_identity(request, payload.agent_id)
    sig = _sha256(payload.target_name, payload.why_useful)
    api_key_id = _get_api_key_id(request)

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool="recipify-request",
        signature=sig,
    )

    if not rl.allowed:
        if rl.deduped:
            return RecipifyRequestOut(
                ok=True,
                id="",
                issue_url=rl.issue_url,
                deduped=True,
            )
        if rl.loop_block:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "loop_detector_cooldown",
                    "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
                },
            )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "force_available": rl.force_available,
                "last_submissions": rl.last_submissions,
            },
        )

    # Persist (durable write first)
    row = RecipifyRequest(
        target_name=payload.target_name,
        why_useful=payload.why_useful,
        suggested_sources=payload.suggested_sources,
        agent_id=payload.agent_id,
        api_key_id=api_key_id,
        signature=sig,
        issue_url="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # GitHub dispatch (best-effort)
    gh_url = (
        github_dispatch.dispatch_event(
            "recipify-request",
            {
                "id": str(row.id),
                "target_name": payload.target_name,
                "why_useful": payload.why_useful,
                "suggested_sources": payload.suggested_sources,
                "agent_id": payload.agent_id,
                "signature": sig,
            },
        )
        or ""
    )

    if gh_url:
        row.issue_url = gh_url
        db.commit()
        feedback_ratelimit.update_dedup_url(sig, gh_url)

    logger.info("recipify-request accepted: id=%s sig=%s", row.id, sig[:12])
    return RecipifyRequestOut(ok=True, id=str(row.id), issue_url=gh_url)


@router.post(
    "/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
)
def post_feedback(
    payload: FeedbackIn,
    request: Request,
    db: Session = Depends(get_db),
) -> FeedbackOut:
    """Submit a feedback entry for a skill or recipe."""
    identity = _get_identity(request, payload.agent_id)
    sig = _sha256(payload.category, payload.message)
    api_key_id = _get_api_key_id(request)

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool="feedback",
        signature=sig,
        force=payload.force,
        confirmation=payload.confirmation,
    )

    if not rl.allowed:
        if rl.deduped:
            return FeedbackOut(
                ok=True,
                id="",
                issue_url=rl.issue_url,
                deduped=True,
            )
        if rl.loop_block:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "loop_detector_cooldown",
                    "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
                },
            )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "force_available": rl.force_available,
                "last_submissions": rl.last_submissions,
            },
        )

    # Persist
    row = FeedbackSubmission(
        category=payload.category,
        message=payload.message,
        context=payload.context,
        agent_id=payload.agent_id,
        api_key_id=api_key_id,
        signature=sig,
        issue_url="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # GitHub dispatch (best-effort)
    gh_url = (
        github_dispatch.dispatch_event(
            "feedback",
            {
                "id": str(row.id),
                "category": payload.category,
                "message": payload.message,
                "context": payload.context,
                "agent_id": payload.agent_id,
                "signature": sig,
            },
        )
        or ""
    )

    if gh_url:
        row.issue_url = gh_url
        db.commit()
        feedback_ratelimit.update_dedup_url(sig, gh_url)

    logger.info("feedback accepted: id=%s cat=%s sig=%s", row.id, payload.category, sig[:12])
    return FeedbackOut(ok=True, id=str(row.id), issue_url=gh_url)
