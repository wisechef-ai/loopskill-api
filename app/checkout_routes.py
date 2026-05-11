"""Stripe Checkout subscription routes for Recipes by WiseChef.

POST /api/checkout/{tier}     — create a Stripe Checkout Session for a tier
GET  /api/billing/me          — current user's subscription state (cookie auth)
"""
from __future__ import annotations

import logging
import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.config import settings
from app.database import get_db
from app.models import User
from app.subscription_service import (
    SubscriptionError,
    TIER_PRICE_IDS,
    _apply_subscription_state,
    create_checkout_session,
    downgrade_pro_plus_to_pro,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["checkout"])

# ── Phase 2: billing/me reconciliation constants ─────────────────────────────

# Maximum seconds we are willing to wait for Stripe during /api/billing/me.
# Passed as the Stripe SDK per-call timeout so a slow Stripe API can't hang
# the success-page poll indefinitely.
BILLING_ME_RECONCILE_BUDGET_S: int = 4

# Minimum gap (seconds) between reconciliation attempts for the same user.
# Prevents a hammering frontend from burning through our Stripe read quota.
_RECONCILE_COOLDOWN_S: int = 5

# In-process cache: user_id (str) → monotonic timestamp of last attempt.
# Fine-grained enough for our purpose; resets on worker restart (acceptable).
_reconcile_last_attempt: Dict[str, float] = {}

# Subscription statuses that are considered "in-sync" — no reconcile needed.
_HEALTHY_STATUSES = frozenset({"active", "trialing"})


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

    # Legacy tier URL alias rewrite — keeps old /api/checkout/cook etc. working.
    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    LEGACY_TIER_URL_ALIASES = {"cook": "pro", "operator": "pro_plus", "studio": "pro_plus"}
    if tier in LEGACY_TIER_URL_ALIASES:
        logger.info("Legacy tier URL %r → rewriting to %r", tier, LEGACY_TIER_URL_ALIASES[tier])
        tier = LEGACY_TIER_URL_ALIASES[tier]

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
    promo_code = body.get("promo_code") if isinstance(body, dict) else None

    try:
        result = create_checkout_session(
            user=user,
            tier=tier,
            db=db,
            success_url=success_url,
            cancel_url=cancel_url,
            promo_code=promo_code,
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
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Current authenticated user's subscription state.

    Phase 2: if the user has a Stripe customer ID but no active tier in the DB
    (race condition window after checkout, before webhook delivery), perform an
    inline Stripe lookup and apply the subscription synchronously.  This makes
    the success-page poll converge in one RTT rather than waiting up to 30 s for
    the webhook to arrive.

    Guards:
    - Entire Stripe call is wrapped in try/except; any exception falls back to
      the stale DB state so the endpoint never 5xx.
    - Per-user cooldown (``_RECONCILE_COOLDOWN_S``) prevents a hammering
      frontend from exhausting Stripe quota.
    - ``BILLING_ME_RECONCILE_BUDGET_S`` is passed as Stripe call timeout.
    - Never creates customers, subscriptions, or invoices — read + sync only.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")

    # ── Phase 2: server-side reconciliation ──────────────────────────────────
    needs_reconcile = (
        bool(user.stripe_customer_id)
        and (
            user.subscription_tier is None
            or user.subscription_status not in _HEALTHY_STATUSES
        )
    )
    if needs_reconcile:
        user_key = str(user.id)
        now = time.monotonic()
        last = _reconcile_last_attempt.get(user_key, 0.0)
        if now - last >= _RECONCILE_COOLDOWN_S:
            _reconcile_last_attempt[user_key] = now
            try:
                import stripe
                stripe.api_key = settings.STRIPE_SECRET_KEY
                stripe.api_version = "2026-01-28.clover"
                subs = stripe.Subscription.list(
                    customer=user.stripe_customer_id,
                    status="active",
                    limit=1,
                    expand=["data.items.data.price"],
                    timeout=BILLING_ME_RECONCILE_BUDGET_S,
                )
                data = (subs or {}).get("data") or []
                if data:
                    sub = data[0]
                    _apply_subscription_state(user, dict(sub), db)
                    db.refresh(user)
                    logger.info(
                        "billing/me reconciled user %s → tier=%s status=%s",
                        user.id,
                        user.subscription_tier,
                        user.subscription_status,
                    )
            except Exception:
                logger.warning(
                    "billing/me reconciliation failed for user %s — returning stale DB state",
                    user.id,
                    exc_info=True,
                )
        else:
            logger.debug(
                "billing/me reconciliation skipped for user %s (cooldown, %.1fs remaining)",
                user.id,
                _RECONCILE_COOLDOWN_S - (now - last),
            )

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
    """Switch a Pro+ subscriber to Pro with proration.

    Requires authentication. Returns 400 if the caller isn't currently on pro_plus.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    try:
        return downgrade_pro_plus_to_pro(user, db)
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
