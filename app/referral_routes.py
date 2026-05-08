"""Referral routes for WiseRecipes.

Endpoints:
  GET /api/me/referral-code  — return current user's referral code
  GET /api/me/referrals      — list referred users + lifetime earnings
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.database import get_db
from app.models import User, Referral, CreatorPayout
from app.referral import ensure_referral_code

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["referrals"])


def _require_auth(user: Optional[User]) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    return user


@router.get("/me/referral-code")
async def get_referral_code(
    request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Return the authenticated user's referral code.

    Auto-generates one on first access.
    """
    user = _require_auth(user)
    code = ensure_referral_code(user, db)
    return {"referral_code": code}


@router.get("/me/referrals")
async def list_referrals(
    request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """List referred users and referral earnings for the authenticated user."""
    user = _require_auth(user)

    # Referred users
    referrals = (
        db.query(Referral)
        .filter(Referral.referrer_user_id == user.id)
        .order_by(Referral.created_at.desc())
        .all()
    )

    referred_users = []
    for r in referrals:
        referred_name = None
        if r.referred_user_id:
            referred = db.query(User).filter(User.id == r.referred_user_id).first()
            if referred:
                referred_name = referred.display_name
        referred_users.append({
            "id": str(r.id),
            "referred_user_id": str(r.referred_user_id) if r.referred_user_id else None,
            "referred_name": referred_name,
            "referred_email": r.referred_email,
            "status": r.status,
            "rate": float(r.rate),
            "reward_cents": r.reward_cents,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "converted_at": r.converted_at.isoformat() if r.converted_at else None,
        })

    # Lifetime referral earnings from creator_payouts (tagged via metadata)
    # We store referral earnings as creator_payouts rows — sum them up
    # For now, sum reward_cents from converted referrals
    lifetime_cents = (
        db.query(func.coalesce(func.sum(Referral.reward_cents), 0))
        .filter(
            Referral.referrer_user_id == user.id,
            Referral.status == "converted",
        )
        .scalar()
    )

    return {
        "referral_code": user.referral_code or ensure_referral_code(user, db),
        "referred_count": len(referrals),
        "lifetime_earnings_cents": lifetime_cents,
        "referrals": referred_users,
    }
