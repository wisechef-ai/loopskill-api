"""Admin routes — master-key gated operations.

POST /api/admin/reindex-all — catastrophic BM25 recovery, reindexes all skills.
GET  /api/admin/skill-publish-requests/{id}/tarball — return raw tarball BYTEA
     for a skill publish request (admin review only).
"""

from __future__ import annotations

import logging
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
