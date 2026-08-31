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
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FederationRegistryProposal, FeedbackSubmission, RecipifyRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal"])


# ── Schemas ────────────────────────────────────────────────────────────────


class IssueUrlPatch(BaseModel):
    issue_url: str
    table: Literal["feedback", "recipify", "federation"]


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

    logger.info(
        "issue url patched: table=%s id=%s url=%s",
        body.table,
        row_id,
        body.issue_url,
    )
    return {"ok": True, "id": str(row_id), "issue_url": body.issue_url}
