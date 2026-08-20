"""Stripe Checkout subscription routes for LoopSkill.

POST /api/checkout/{tier}     — create a Stripe Checkout Session for a tier
GET  /api/billing/me          — current user's subscription state (cookie auth)
"""

from __future__ import annotations

import logging
import time

import stripe
from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.config import settings
from app.database import get_db
from app.models import User
from app.subscription_service import (
    TIER_PRICE_IDS,
    SubscriptionError,
    _apply_subscription_state,
    create_checkout_session,
    downgrade_pro_plus_to_pro,
)
from app.services.bundle_quota import quota_status
from app.tier_labels import api_key_cap as _tier_api_key_cap

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
# Bounded TTLCache: evicts entries after 4× the cooldown so worker memory is
# always capped (Issue #20 — secfix_1905/H).
_reconcile_last_attempt: TTLCache[str, float] = TTLCache(maxsize=10_000, ttl=_RECONCILE_COOLDOWN_S * 4)

# Subscription statuses that are considered "in-sync" — no reconcile needed.
_HEALTHY_STATUSES = frozenset({"active", "trialing"})


def _reject_agent_principals(request: Request, user: User | None = None) -> None:
    """403 a self-registered agent before any Stripe object is created.

    agentreg_0819 (review round 2, F5c). ``POST /api/agents/register`` enrols an
    autonomous agent with no human in the loop, and its shadow ``User`` carries
    ``is_agent=True``. Such a principal must never open a Stripe Checkout or
    Customer Portal session: those flows exist to collect a real payment
    instrument from a real person who can consent to a recurring charge, and a
    self-enrolled agent is by construction neither.

    Today an agent ALSO fails ``get_current_user_optional`` — it has no OAuth
    session and never can. So this gate is not currently what stops it; it is
    what stops it LEGIBLY, and what keeps stopping it if these routes ever start
    accepting an ``x-api-key``. A 401 ``login_required`` tells an agent to go
    get a session, which is advice it cannot act on and should not act on. 403
    says "not for this kind of caller", which is the truth, and it makes the
    boundary assertable — ``tests/test_agentreg_0819_agent_self_registration.py``
    no longer accepts "401 or 404" as evidence of a fence, because a route that
    does not exist answers exactly the same way.

    Three sources are checked because these paths sit behind
    ``JWT_AUTH_PREFIXES``, which returns from ``APIKeyMiddleware`` BEFORE any
    ``auth_ctx`` is stamped — so reading ``request.state`` alone would find
    nothing and silently pass:

    1. the resolved ``User`` (the durable ``is_agent`` column), when the caller
       reached us with a session at all;
    2. ``request.state.auth_ctx``, for any future path that does stamp one;
    3. the ``x-api-key`` header, resolved through the middleware's own helper —
       this is the branch that actually fires today.
    """
    if user is not None and bool(getattr(user, "is_agent", False)):
        raise HTTPException(status_code=403, detail="agent_principals_cannot_transact")

    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None:
        from app.middleware.api_key import _auth_ctx_from_api_key

        ctx = _auth_ctx_from_api_key(request)
    if getattr(ctx, "is_agent", False):
        raise HTTPException(status_code=403, detail="agent_principals_cannot_transact")


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

    A self-registered agent principal (``AuthContext.is_agent``) gets 403 — see
    :func:`_reject_agent_principals`. Agents must not create Stripe sessions.
    """
    _reject_agent_principals(request, user)
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
    # Rationale: request body is optional JSON; malformed body → use defaults
    except Exception:  # noqa: BLE001
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
            utm_ref=request.cookies.get("recipes_utm_ref"),
        )
    except SubscriptionError as e:
        logger.error("Checkout creation failed for user %s tier %s: %s", user.id, tier, e)
        raise HTTPException(status_code=400, detail=str(e))
    except stripe.InvalidRequestError as e:
        # fix/checkout-hardening (2026-07-17): Stripe hard-forbids mixing
        # currencies on one customer while ANY subscription is active. A
        # leftover sub in another currency (e.g. a EUR e2e test sub from the
        # recipes drills) made every USD checkout die as an opaque 500.
        # Surface it as an actionable 409 instead so the user/support can act.
        msg = str(e)
        if "combine currencies" in msg or "cannot combine" in msg.lower():
            logger.error(
                "Checkout currency conflict for user %s tier %s (customer has an "
                "active subscription/session in another currency): %s",
                user.id,
                tier,
                msg,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "currency_conflict",
                    "message": (
                        "Your billing profile has an active subscription in a "
                        "different currency, which blocks this checkout. "
                        "Contact support to resolve it."
                    ),
                },
            )
        logger.exception("Stripe InvalidRequestError for user %s tier %s", user.id, tier)
        raise HTTPException(status_code=500, detail="checkout_error")
    # Rationale: unexpected Stripe/DB error during checkout; surface as 500
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected checkout error for user %s tier %s", user.id, tier)
        raise HTTPException(status_code=500, detail="checkout_error")

    return result


@router.get("/checkout/{tier}")
async def checkout_get_redirect(tier: str):
    """Redirect browser GETs on the POST-only checkout route to /pricing.

    fix/checkout-hardening (2026-07-17): the pricing page's sign-in CTA links
    to ``/signin?next=/api/checkout/pro`` — after OAuth the browser lands here
    with a GET and used to hit a bare 405 Method Not Allowed dead-end (Adam
    reproduced it live). A GET can never create a Checkout Session (the JSON
    body + CSRF posture live on the POST), so the honest behaviour is to send
    the browser back to /pricing where the real upgrade button is.
    """
    del tier  # every tier variant redirects to the same pricing surface
    return RedirectResponse(url="/pricing", status_code=303)


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
    needs_reconcile = bool(user.stripe_customer_id) and (
        user.subscription_tier is None or user.subscription_status not in _HEALTHY_STATUSES
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
            # Rationale: Stripe reconciliation is best-effort; any error → return stale DB state
            except Exception:  # noqa: BLE001
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

    _quota = quota_status(db, user.id, user.subscription_tier)
    return {
        "user_id": str(user.id),
        "email": user.email,
        "stripe_customer_id": user.stripe_customer_id,
        "subscription_id": user.subscription_id,
        "subscription_status": user.subscription_status,
        "subscription_tier": user.subscription_tier,
        # cookbook_limit — SSOT in config/tiers.yaml, read through the SAME
        # helper the create route enforces with (app.services.bundle_quota), so
        # the portal library page can never render a cap the API does not hold
        # the user to. Only PRIVATE bundles are metered (D-011); the additive
        # max_private_bundles / private_bundles_used keys say so out loud while
        # cookbook_limit stays for existing portal readers (dual-accept).
        "cookbook_limit": _quota["limit"],  # compat-alias
        "max_private_bundles": _quota["limit"],
        "private_bundles_used": _quota["used"],
        # api_key_cap — SSOT in config/tiers.yaml (bundles_0811 P2.5), read
        # through the SAME helper app/api_key_routes.py enforces with
        # (app.tier_labels.api_key_cap). account.astro's #key-tier-note
        # currently hardcodes "Pro: 1 key." — the portal must render THIS
        # field instead: GET /api/billing/me -> api_key_cap (int).
        "api_key_cap": _tier_api_key_cap(user.subscription_tier),
        "subscription_current_period_end": (
            user.subscription_current_period_end.isoformat() if user.subscription_current_period_end else None
        ),
    }


@router.post("/subscriptions/downgrade")
async def downgrade_subscription(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Switch a Pro+ subscriber to Pro with proration.

    Requires authentication. Returns 400 if the caller isn't currently on pro_plus.
    A self-registered agent principal gets 403 — see :func:`_reject_agent_principals`.
    """
    _reject_agent_principals(request, user)
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    try:
        return downgrade_pro_plus_to_pro(user, db)
    except SubscriptionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Rationale: downgrade() raises SubscriptionError normally; fallback handles import-reload edge case
    except Exception as e:  # noqa: BLE001
        # Defensive: if isinstance check above didn't catch a SubscriptionError due to
        # module-reload or import-cycle weirdness, match by class name as a backup.
        if type(e).__name__ == "SubscriptionError":
            raise HTTPException(status_code=400, detail=str(e))
        logger.exception("Downgrade failed for user %s", user.id)
        raise HTTPException(status_code=500, detail="downgrade_error")


@router.post("/billing/portal-session")
async def create_billing_portal_session(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
):
    """Create a Stripe Customer Portal session for self-serve billing.

    A self-registered agent principal gets 403 — see :func:`_reject_agent_principals`.
    """
    _reject_agent_principals(request, user)
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
            idempotency_key=f"portal_session_{user.stripe_customer_id}",
        )
    # Rationale: Stripe portal session can fail for many reasons; surface as 500
    except Exception as e:  # noqa: BLE001
        logger.exception("Stripe portal session creation failed for user %s", user.id)
        raise HTTPException(status_code=500, detail=f"portal_error:{e}")

    return {"url": session["url"]}
