"""Public status endpoint for feedback and recipify-request submissions.

Allows callers to poll the status of a previously submitted feedback or
recipify-request row after the initial POST. The issue_url is populated
asynchronously by the GitHub Actions workflow via the internal PATCH endpoint.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FeedbackSubmission, IncidentReport, RecipifyRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feedback-status"])

# ── Simple in-process rate limit (public endpoint — tighter ceiling) ─────────

_status_limit_lock = __import__("threading").Lock()
_status_hits: dict[str, list[float]] = defaultdict(list)

_STATUS_MAX = 20  # per identity per minute
_STATUS_WINDOW_S = 60.0


def _check_status_rate_limit(request: Request) -> None:
    """Simple per-IP rate limiter for the public status endpoint."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        identity = f"ip:{forwarded.split(',')[0].strip()}"
    elif request.client:
        identity = f"ip:{request.client.host}"
    else:
        identity = "ip:unknown"

    now = time.monotonic()
    cutoff = now - _STATUS_WINDOW_S

    with _status_limit_lock:
        hits = [t for t in _status_hits.get(identity, []) if t > cutoff]
        if len(hits) >= _STATUS_MAX:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": "rate_limit_exceeded"},
            )
        hits.append(now)
        _status_hits[identity] = hits


# ── Schema ─────────────────────────────────────────────────────────────────


class FeedbackStatusOut(BaseModel):
    id: str
    status: str
    issue_url: str
    created_at: datetime


# ── Endpoint ───────────────────────────────────────────────────────────────


@router.get(
    "/api/feedback/{row_id}",
    response_model=FeedbackStatusOut,
)
def get_feedback_status(
    row_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> FeedbackStatusOut:
    """Return the current status and issue_url for a feedback, recipify or incident row.

    Looks up the row_id in feedback_submissions, recipify_requests and
    incident_reports. Returns 404 if no row matches.
    """
    _check_status_rate_limit(request)

    row = db.query(FeedbackSubmission).filter(FeedbackSubmission.id == row_id).first()
    if row is None:
        row = db.query(RecipifyRequest).filter(RecipifyRequest.id == row_id).first()
    # The skill-error contract (app/mcp/tools/skill_error.py) hands the caller an
    # *incident_reports* id and tells clients to poll THIS endpoint for the
    # resolved issue URL. incident_reports was never queried here, so every
    # skill-error poll was a 404 and the URL was unreachable by design.
    if row is None:
        row = db.query(IncidentReport).filter(IncidentReport.id == row_id).first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feedback row not found",
        )

    # FeedbackSubmission and RecipifyRequest both carry a non-null `created_at`, so
    # this line was safe as long as the handler could only see those two tables.
    # IncidentReport has NO created_at — its only timestamp is `occurred_at` — so
    # the moment incident ids began resolving this raised AttributeError and the
    # public poll endpoint 500'd for every skill-error report. Resolve per model
    # instead of assuming a column shared by all three.
    created_at = row.created_at if hasattr(row, "created_at") else row.occurred_at

    return FeedbackStatusOut(
        id=str(row.id),
        status=getattr(row, "feedback_status", "pending") or "pending",
        issue_url=getattr(row, "issue_url", "") or "",
        created_at=created_at,
    )
