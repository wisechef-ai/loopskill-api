"""Internal PATCH endpoint for GitHub workflow to post back real issue URLs.

Gated by ``X-Internal-Token`` header matched against the ``INTERNAL_PATCH_TOKEN``
environment variable. Only intended to be called by the GitHub Actions workflow
after it creates the GitHub issue and knows the real URL.
"""

from __future__ import annotations

import logging
import os
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FederationRegistryProposal, FeedbackSubmission, IncidentReport, RecipifyRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal"])


# ── Schemas ────────────────────────────────────────────────────────────────


class IssueUrlPatch(BaseModel):
    # Bound both fields: values land in a Postgres TEXT column and, for
    # signature, in the in-process _dedup dict, which has no total-size cap.
    # The workflow already truncates the signature to 64 hex chars and GitHub
    # issue URLs are ~60 chars, so legitimate traffic never hits these limits.
    issue_url: str = Field(max_length=2048)
    # "incident" = incident_reports, the table both skill-error writers persist.
    # Before this member existed the workflow had no correct table to name for a
    # skill-error event: it sent table="feedback", the handler looked the id up in
    # feedback_submissions, matched nothing, and answered 404 — for 100% of
    # skill-error dispatches. incident_reports also had no column to hold a URL.
    table: Literal["feedback", "recipify", "federation", "incident"]
    # Composite signature from the dispatch payload. Optional; when present the
    # rate-limiter dedup cache is backfilled so a repeat report returns the real
    # issue URL instead of "" (see app.feedback_ratelimit.update_dedup_url).
    signature: str | None = Field(default=None, max_length=64)


# ── Auth helper ────────────────────────────────────────────────────────────


def _verify_token(x_internal_token: str = Header(default="")) -> None:
    """Validate X-Internal-Token against INTERNAL_PATCH_TOKEN env var."""
    expected = os.environ.get("INTERNAL_PATCH_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


# ── Endpoint ───────────────────────────────────────────────────────────────


@router.patch(
    "/feedback/{row_id}/issue-url",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_verify_token)],
)
def patch_issue_url(
    row_id: UUID,
    body: IssueUrlPatch,
    db: Session = Depends(get_db),
) -> dict:
    """Update issue_url and set feedback_status='filed' on the matching row.

    Called by the GitHub Actions workflow after it creates the GitHub issue
    and knows the real issue URL. Gated by X-Internal-Token header.
    """
    if body.table == "feedback":
        row = db.query(FeedbackSubmission).filter(FeedbackSubmission.id == row_id).first()
    elif body.table == "federation":
        row = db.query(FederationRegistryProposal).filter(FederationRegistryProposal.id == row_id).first()
    elif body.table == "incident":
        row = db.query(IncidentReport).filter(IncidentReport.id == row_id).first()
    else:
        row = db.query(RecipifyRequest).filter(RecipifyRequest.id == row_id).first()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Row not found")

    row.issue_url = body.issue_url
    # FederationRegistryProposal tracks lifecycle via `status`
    # (pending/accepted/rejected), not `feedback_status` — leave it untouched
    # here; it only changes when a maintainer accepts/rejects the proposal.
    if body.table != "federation":
        row.feedback_status = "filed"
    db.commit()

    # Backfill the rate-limiter dedup cache.
    #
    # check_and_record() stores every accepted hit with an EMPTY issue_url
    # placeholder. The 4 sibling MCP tools that dispatch to GitHub call
    # feedback_ratelimit.update_dedup_url() to fill it in, because they learn the
    # issue URL synchronously inside the tool call. skill_error.py cannot: it
    # dispatches and returns status="pending" with issue_url="" while the workflow
    # files the issue asynchronously. It is therefore the ONE tool with no caller
    # for update_dedup_url, so its dedup soft-block (2nd identical signature inside
    # 7d) replayed issue_url="" — telling a repeat reporter "already reported" with
    # no link to the existing issue. This endpoint is where the URL first becomes
    # known, so the backfill belongs here.
    if body.signature:
        from app import feedback_ratelimit

        feedback_ratelimit.update_dedup_url(body.signature, body.issue_url)

    logger.info(
        "issue url patched: table=%s id=%s url=%s",
        body.table,
        row_id,
        body.issue_url,
    )
    return {"ok": True, "id": str(row_id), "issue_url": body.issue_url}
