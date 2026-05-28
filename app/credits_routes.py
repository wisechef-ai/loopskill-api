"""app/credits_routes.py — subscriber-credit endpoints.

GET /api/me/credits
  Requires JWT or Bearer authentication.
  Returns the caller's subscriber credits, most recent first.

Response shape per credit:
  {
    "id":          "<uuid>",
    "type":        "contributor_discount",
    "amount_pct":  50,
    "granted_at":  "2026-05-28T00:00:00+00:00",
    "expires_at":  "2026-11-24T00:00:00+00:00",
    "used_at":     null | "<iso-datetime>",
    "status":      "active" | "used" | "expired"
  }

Status derivation (client-friendly computed field):
  - "used"    → used_at IS NOT NULL
  - "expired" → used_at IS NULL AND expires_at < NOW()
  - "active"  → used_at IS NULL AND expires_at >= NOW()
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me", tags=["credits"])


# ── Pydantic schemas ──────────────────────────────────────────────────────


class CreditOut(BaseModel):
    id: str
    type: str
    amount_pct: int
    granted_at: str
    expires_at: str
    used_at: str | None
    status: str


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.get("/credits", response_model=list[CreditOut])
def list_my_credits(
    request: Request,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return the authenticated user's subscriber credits, newest first.

    Authentication: JWT cookie or Authorization: Bearer <token>.
    Returns 401 if the caller is not authenticated.
    """
    from app.models import SubscriberCredit

    user = get_current_user_optional(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")

    credits = (
        db.query(SubscriberCredit)
        .filter(SubscriberCredit.user_id == user.id)
        .order_by(SubscriberCredit.granted_at.desc())
        .all()
    )

    now = datetime.now(UTC)
    result: list[dict[str, Any]] = []
    for c in credits:
        if c.used_at is not None:
            status = "used"
        elif c.expires_at < now:
            status = "expired"
        else:
            status = "active"

        result.append(
            {
                "id": str(c.id),
                "type": c.type,
                "amount_pct": c.amount_pct,
                "granted_at": c.granted_at.isoformat() if c.granted_at else "",
                "expires_at": c.expires_at.isoformat(),
                "used_at": c.used_at.isoformat() if c.used_at else None,
                "status": status,
            }
        )

    return result
