"""Stripe Checkout subscription routes for Recipes by WiseChef.

POST /api/checkout/{tier}     — create a Stripe Checkout Session for a tier
GET  /api/billing/me          — current user's subscription state (cookie auth)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.config import settings
from app.database import get_db
from app.models import User
from app.subscription_service import (
    SubscriptionError,
    TIER_PRICE_IDS,
    create_checkout_session,
    downgrade_studio_to_cook,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["checkout"])


@router.post("/checkout/{tier}")
async def create_subscription_checkout(
    tier: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Create a Stripe Checkout Session for the given subscription tier.

    Requires the user to be authenticated (JWT cookie set by /api/auth/{provider}/callback).
    Anonymous users get 401 with a hint to log in.
    """
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="login_required",
        )
    if tier not in TIER_PRICE_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_tier:{tier}. Valid: {sorted(TIER_PRICE_IDS)}",
        )

    body = {}
    try:
        body = await request.json()
    except Exception:
        # No body is fine — defaults will be used
        pass
    success_url = body.get("success_url") if isinstance(body, dict) else None
    cancel_url = body.get("cancel_url") if isinstance(body, dict) else None

    try:
        result = create_checkout_session(
            user=user,
            tier=tier,
            db=db,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except SubscriptionError as e:
        logger.error("Checkout creation failed for user %s tier %s: %s", user.id, tier, e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected checkout error for user %s tier %s", user.id, tier)
        raise HTTPException(status_code=500, detail="checkout_error")

    return result


@router.get("/billing/me")
async def billing_me(
    user: User | None = Depends(get_current_user_optional),
):
    """Current authenticated user's subscription state."""
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    return {
        "user_id": str(user.id),
        "email": user.email,
        "stripe_customer_id": user.stripe_customer_id,
        "subscription_id": user.subscription_id,
        "subscription_status": user.subscription_status,
        "subscription_tier": user.subscription_tier,
        "subscription_current_period_end": (
            user.subscription_current_period_end.isoformat()
            if user.subscription_current_period_end else None
        ),
    }


@router.post("/subscriptions/downgrade")
async def downgrade_subscription(
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Switch an All-in (studio) subscriber to Pro (cook) with proration.

    Requires authentication. Returns 400 if the caller isn't currently on studio.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    try:
        return downgrade_studio_to_cook(user, db)
    except SubscriptionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Defensive: if isinstance check above didn't catch a SubscriptionError due to
        # module-reload or import-cycle weirdness, match by class name as a backup.
        if type(e).__name__ == "SubscriptionError":
            raise HTTPException(status_code=400, detail=str(e))
        logger.exception("Downgrade failed for user %s", user.id)
        raise HTTPException(status_code=500, detail="downgrade_error")


@router.post("/billing/portal-session")
async def create_billing_portal_session(
    user: User | None = Depends(get_current_user_optional),
):
    """Create a Stripe Customer Portal session for self-serve billing."""
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    if not user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="no_subscription")

    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = "2026-01-28.clover"

    base = (settings.OAUTH_REDIRECT_BASE or "").rstrip("/")
    return_url = f"{base}/library" if base else "/library"

    try:
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=return_url,
        )
    except Exception as e:
        logger.exception("Stripe portal session creation failed for user %s", user.id)
        raise HTTPException(status_code=500, detail=f"portal_error:{e}")

    return {"url": session["url"]}
