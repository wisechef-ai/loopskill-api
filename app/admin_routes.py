"""Admin routes — master-key gated operations.

POST /api/admin/reindex-all — catastrophic BM25 recovery, reindexes all skills.
GET  /api/admin/skill-publish-requests/{id}/tarball — return raw tarball BYTEA
     for a skill publish request (admin review only).
PATCH /api/admin/skill-publish-requests/{id}/status — approve or reject a
     pending skill-publish request; approval triggers a contributor-discount
     credit grant for qualifying (pro/pro_plus) authors.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.search_index import reindex_all

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ReindexAllResponse(BaseModel):
    reindexed: int


@router.post("/reindex-all", response_model=ReindexAllResponse)
def admin_reindex_all(
    request: Request,
    db: Session = Depends(get_db),
):
    """Reindex BM25 search_vector for every non-archived skill.

    Master-key only (api_key_user_id must be None).  For catastrophic
    recovery only — normal publishes auto-reindex.
    """
    # Master-key only: api_key_user_id must be None
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")

    count = reindex_all(db)
    logger.info("admin reindex-all: reindexed %d skills", count)
    return ReindexAllResponse(reindexed=count)


@router.get("/skill-publish-requests/{request_id}/tarball")
def admin_get_publish_request_tarball(
    request_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    """Return the raw tarball bytes for a SkillPublishRequest.

    Master-key only — used by the reviewer to inspect skill content locally
    and by the skill-publish-approver workflow to fetch the tarball for
    final publishing.
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")

    from app.models import SkillPublishRequest

    row = db.query(SkillPublishRequest).filter(SkillPublishRequest.id == request_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Publish request not found")
    if not row.tarball_bytes:
        raise HTTPException(status_code=404, detail="Tarball not stored for this request")

    return Response(
        content=row.tarball_bytes,
        media_type="application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="{row.slug}-{row.version}.tar.gz"',
            "X-SHA256": row.sha256 or "",
        },
    )


# ── Skill-publish-request approval / rejection ────────────────────────────


class UpdatePublishRequestStatusIn(BaseModel):
    status: Literal["approved", "rejected"]
    reviewed_by: str | None = None  # e.g. GitHub username of the reviewer
    reject_reason: str | None = None  # required when status == "rejected"


class UpdatePublishRequestStatusOut(BaseModel):
    id: str
    status: str
    reviewed_at: str
    credit_granted: bool


@router.patch(
    "/skill-publish-requests/{request_id}/status",
    response_model=UpdatePublishRequestStatusOut,
    status_code=200,
)
def admin_update_publish_request_status(
    request_id: UUID,
    body: UpdatePublishRequestStatusIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Approve or reject a pending skill-publish request.

    Master-key only.  On approval:
      - Sets status = 'approved' and records reviewed_at / reviewed_by.
      - Calls grant_contributor_credit() for the requester if they are
        a pro/pro_plus subscriber with no existing unused credit.

    On rejection:
      - Sets status = 'rejected' and persists the reject_reason.
      - No credit is granted.
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")

    from app.models import SkillPublishRequest

    row = db.query(SkillPublishRequest).filter(SkillPublishRequest.id == request_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Publish request not found")

    if row.status not in ("pending",):
        raise HTTPException(
            status_code=409,
            detail=f"Publish request is already in status '{row.status}'; cannot update",
        )

    if body.status == "rejected" and not body.reject_reason:
        raise HTTPException(
            status_code=422,
            detail="reject_reason is required when rejecting a publish request",
        )

    now = datetime.now(UTC)
    row.status = body.status
    row.reviewed_at = now
    row.reviewed_by = body.reviewed_by
    if body.status == "rejected":
        row.reject_reason = body.reject_reason

    db.flush()

    credit_granted = False
    if body.status == "approved" and row.requester_user_id is not None:
        # Resolve the skill_id for the published slug so we can pass it to
        # the credit service.  Absence of the skill row is non-fatal — the
        # credit grant is best-effort and must not block the status update.
        from app.models import Skill
        from app.subscriber_credit_service import grant_contributor_credit

        skill = db.query(Skill).filter(Skill.slug == row.slug).first()
        skill_id = skill.id if skill is not None else None

        # Rationale: credit grant failure (e.g. user not pro, already has credit)
        # must never roll back the approval — log and continue.
        try:
            credit = grant_contributor_credit(
                db=db,
                user_id=row.requester_user_id,
                skill_id=skill_id,
            )
            credit_granted = credit is not None
        # Rationale: credit grant failure must never roll back the approval — log and continue
        except Exception:  # noqa: BLE001
            logger.exception(
                "admin_update_publish_request_status: credit grant failed for "
                "user=%s skill_slug=%s (non-fatal)",
                row.requester_user_id,
                row.slug,
            )

    db.commit()

    logger.info(
        "admin_update_publish_request_status: request=%s status=%s reviewer=%s credit=%s",
        request_id,
        body.status,
        body.reviewed_by,
        credit_granted,
    )

    return UpdatePublishRequestStatusOut(
        id=str(row.id),
        status=row.status,
        reviewed_at=now.isoformat(),
        credit_granted=credit_granted,
    )
