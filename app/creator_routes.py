"""Creator-facing API routes — authentication, Stripe Connect onboarding, and earnings dashboard.

Endpoints:
  GET  /api/auth/github          — Redirect to GitHub OAuth
  GET  /api/auth/callback        — GitHub OAuth callback
  GET  /api/auth/me              — Current user profile
  POST /api/stripe/onboard       — Start Stripe Connect onboarding
  GET  /api/stripe/status        — Check Stripe Connect account status
  GET  /api/stripe/dashboard     — Get Stripe Express dashboard link
  GET  /api/creator/earnings     — Earnings summary + breakdown
  GET  /api/creator/payouts      — Payout history
  POST /api/admin/payouts/run    — Trigger monthly payout (admin only)
  POST /api/stripe/webhook       — Stripe webhook handler
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool  # Issue #18
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import (
    AuthError,
    create_jwt,
    exchange_github_code,
    find_or_create_user,
    get_github_auth_url,
    get_user_from_jwt,
    verify_jwt,
)
from app.config import settings
from app.database import get_db
from app.models import Creator, CreatorPayout, Skill, User
from app.payout_engine import compute_monthly_payouts, get_creator_earnings
from app.stripe_service import (
    StripeConnectError,
    create_connect_account,
    create_dashboard_link,
    create_onboarding_link,
    get_account_status,
    verify_webhook_signature,
)
from app.vat import VATResult, calculate_vat, generate_vat_moss_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["creator"])


# ── Auth Dependency ─────────────────────────────────────────────────────

def _get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Extract and verify JWT from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    user = get_user_from_jwt(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _get_api_key(request: Request) -> str:
    """Extract API key from request headers."""
    return request.headers.get("x-api-key", "")


# ── Pydantic Models ─────────────────────────────────────────────────────

class CreatorProfile(BaseModel):
    id: str
    display_name: str
    email: str | None = None
    avatar_url: str | None = None
    stripe_connected: bool = False
    is_creator: bool = False
    is_founder: bool = False
    creator_slug: str | None = None
    token: str | None = None  # JWT (only on login)


class StripeOnboardRequest(BaseModel):
    return_url: str
    refresh_url: str


class StripeStatusResponse(BaseModel):
    connected: bool
    account_id: str | None = None
    charges_enabled: bool = False
    payouts_enabled: bool = False
    details_submitted: bool = False
    country: str | None = None
    currency: str | None = None


class EarningsResponse(BaseModel):
    total_installs: int
    total_gross_cents: int
    total_earned_cents: int
    total_payouts: int
    pending_cents: int
    paid_cents: int
    this_month_installs: int


class PayoutHistoryItem(BaseModel):
    id: str
    period_start: datetime
    period_end: datetime
    installs_count: int
    gross_revenue_cents: int
    creator_share_cents: int
    currency: str
    status: str
    stripe_transfer_id: str | None = None
    paid_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PayoutRunResponse(BaseModel):
    payouts: list[dict]
    total_count: int
    total_cents: int


class VATCalculateRequest(BaseModel):
    amount_cents: int
    country_code: str
    is_b2b: bool = False
    vat_number: str | None = None


class VATCalculateResponse(BaseModel):
    country_code: str
    is_eu: bool
    is_b2b: bool
    vat_rate: float
    vat_cents: int
    gross_cents: int
    net_cents: int
    reverse_charge: bool
    vat_number: str | None = None


class VATMOSSReportResponse(BaseModel):
    report: list[dict]
    total_vat_cents: int


# ── GitHub OAuth ────────────────────────────────────────────────────────

@router.get("/auth/github")
def github_auth_redirect(
    redirect_uri: str = Query(..., description="Frontend callback URL"),
):
    """Redirect to GitHub OAuth authorization page."""
    state = secrets.token_urlsafe(32)
    # In production, state should be stored in Redis/session for CSRF protection
    url = get_github_auth_url(state, redirect_uri)
    return RedirectResponse(url=url)


@router.get("/auth/callback", response_model=CreatorProfile)
async def github_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """Handle GitHub OAuth callback. Exchange code for profile + JWT."""
    try:
        github_data = await exchange_github_code(code)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))

    user = find_or_create_user(db, github_data)

    # Check if user is a creator
    creator = db.query(Creator).filter(Creator.user_id == user.id).first()

    # Generate JWT
    token = create_jwt(user)

    return CreatorProfile(
        id=str(user.id),
        display_name=user.display_name,
        email=user.email,
        avatar_url=user.avatar_url,
        stripe_connected=bool(user.stripe_connect_id),
        is_creator=bool(creator),
        is_founder=creator.is_founder if creator else False,
        creator_slug=creator.slug if creator else None,
        token=token,
    )


# ── Direct token exchange (for frontend SDKs) ──────────────────────────

class TokenExchangeRequest(BaseModel):
    code: str
    state: str


@router.post("/auth/token", response_model=CreatorProfile)
async def exchange_token(
    body: TokenExchangeRequest,
    db: Session = Depends(get_db),
):
    """Exchange GitHub OAuth code for a JWT + user profile (API-friendly)."""
    try:
        github_data = await exchange_github_code(body.code)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))

    user = find_or_create_user(db, github_data)
    creator = db.query(Creator).filter(Creator.user_id == user.id).first()
    token = create_jwt(user)

    return CreatorProfile(
        id=str(user.id),
        display_name=user.display_name,
        email=user.email,
        avatar_url=user.avatar_url,
        stripe_connected=bool(user.stripe_connect_id),
        is_creator=bool(creator),
        is_founder=creator.is_founder if creator else False,
        creator_slug=creator.slug if creator else None,
        token=token,
    )


# ── Profile ─────────────────────────────────────────────────────────────

@router.get("/auth/me", response_model=CreatorProfile)
def get_me(
    request: Request,
    db: Session = Depends(get_db),
):
    """Get current user profile from JWT."""
    user = _get_current_user(request, db)
    creator = db.query(Creator).filter(Creator.user_id == user.id).first()

    return CreatorProfile(
        id=str(user.id),
        display_name=user.display_name,
        email=user.email,
        avatar_url=user.avatar_url,
        stripe_connected=bool(user.stripe_connect_id),
        is_creator=bool(creator),
        is_founder=creator.is_founder if creator else False,
        creator_slug=creator.slug if creator else None,
    )


# ── Stripe Connect — KILLED in top1pct_1105 Phase C ─────────────────────
#
# Plan §0 decision: NO payment for publishers, NO Stripe Connect onboarding.
# Only earning mechanism = 50% referral rev-share via referral code.
#
# These endpoints return 410 Gone so the frontend fails loudly during rollout
# rather than silently succeeding with a no-op.
#
# NOTE: The webhook handler below still processes subscription events — that's
# the *subscription billing* flow (checkout.session.completed etc.) which is
# separate from the creator-payout Stripe Connect flow that's being killed.

@router.post("/stripe/onboard")
def stripe_onboard(
    request: Request,
    db: Session = Depends(get_db),
):
    """KILLED — Stripe Connect creator onboarding removed in top1pct_1105 Phase C.

    Earning mechanism is now 50% referral rev-share only.
    See /referrals for details.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "stripe_connect_removed — creator payouts via Stripe Connect are no longer "
            "offered. Earn 50% recurring rev-share by sharing your referral code. "
            "See /referrals."
        ),
    )


@router.get("/stripe/status")
def stripe_status(
    request: Request,
    db: Session = Depends(get_db),
):
    """KILLED — Stripe Connect status removed in top1pct_1105 Phase C."""
    raise HTTPException(
        status_code=410,
        detail="stripe_connect_removed — see /referrals for the current earning model.",
    )


@router.get("/stripe/dashboard")
def stripe_dashboard_link(
    request: Request,
    db: Session = Depends(get_db),
):
    """KILLED — Stripe Express dashboard removed in top1pct_1105 Phase C."""
    raise HTTPException(
        status_code=410,
        detail="stripe_connect_removed — see /referrals for the current earning model.",
    )


# ── Creator Earnings ────────────────────────────────────────────────────

@router.get("/creator/earnings", response_model=EarningsResponse)
def creator_earnings(
    request: Request,
    db: Session = Depends(get_db),
):
    """Get earnings summary for the authenticated creator."""
    user = _get_current_user(request, db)
    data = get_creator_earnings(db, user.id)
    return EarningsResponse(**data)


@router.get("/creator/payouts", response_model=list[PayoutHistoryItem])
def creator_payouts(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Get payout history for the authenticated creator."""
    user = _get_current_user(request, db)

    query = (
        db.query(CreatorPayout)
        .filter(CreatorPayout.creator_id == user.id)
        .order_by(CreatorPayout.created_at.desc())
    )

    results = query.offset((page - 1) * page_size).limit(page_size).all()

    return [
        PayoutHistoryItem(
            id=str(p.id),
            period_start=p.period_start,
            period_end=p.period_end,
            installs_count=p.installs_count,
            gross_revenue_cents=p.gross_revenue_cents,
            creator_share_cents=p.creator_share_cents,
            currency=p.currency,
            status=p.status,
            stripe_transfer_id=p.stripe_transfer_id,
            paid_at=p.paid_at,
            created_at=p.created_at,
        )
        for p in results
    ]


# ── VAT MOSS ────────────────────────────────────────────────────────────

@router.post("/vat/calculate", response_model=VATCalculateResponse)
def vat_calculate(body: VATCalculateRequest):
    """Calculate VAT MOSS for a given amount and buyer location."""
    result = calculate_vat(
        gross_amount_cents=body.amount_cents,
        buyer_country_code=body.country_code,
        is_b2b=body.is_b2b,
        vat_number=body.vat_number,
    )
    return VATCalculateResponse(
        country_code=result.country_code,
        is_eu=result.is_eu,
        is_b2b=result.is_b2b,
        vat_rate=result.vat_rate,
        vat_cents=result.vat_cents,
        gross_cents=result.gross_cents,
        net_cents=result.net_cents,
        reverse_charge=result.reverse_charge,
        vat_number=result.vat_number,
    )


@router.post("/vat/moss-report", response_model=VATMOSSReportResponse)
def vat_moss_report(
    request: Request,
    db: Session = Depends(get_db),
):
    """Generate a VAT MOSS report for the current period (admin endpoint)."""
    # Basic auth check: require master API key
    api_key = _get_api_key(request)
    if api_key != settings.API_KEY:
        raise HTTPException(status_code=403, detail="Admin access required")

    # Aggregate payouts by country for the current month
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # For now, return a sample report structure
    # TODO: aggregate real transaction data by buyer country when billing is live
    report = generate_vat_moss_report({})
    return VATMOSSReportResponse(
        report=report,
        total_vat_cents=sum(r["vat_cents"] for r in report),
    )


# ── Admin: Payout Runner ────────────────────────────────────────────────

@router.post("/admin/payouts/run", response_model=PayoutRunResponse)
def run_payouts(
    request: Request,
    dry_run: bool = Query(True, description="If true, compute but don't execute"),
    db: Session = Depends(get_db),
):
    """Trigger monthly payout calculation. Requires admin API key."""
    api_key = _get_api_key(request)
    if api_key != settings.API_KEY:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        payouts = compute_monthly_payouts(db, dry_run=dry_run)
    except Exception as e:
        logger.error(f"Payout run failed: {e}")
        raise HTTPException(status_code=500, detail=f"Payout computation failed: {str(e)}")

    return PayoutRunResponse(
        payouts=payouts,
        total_count=len(payouts),
        total_cents=sum(p.get("creator_share_cents", 0) for p in payouts),
    )


# ── Stripe Webhook ──────────────────────────────────────────────────────

@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Stripe webhook events.

    Routes events to the right service:
    - checkout.session.completed, customer.subscription.* → subscription_service
    - account.updated, transfer.* → Connect (creator payouts)

    Idempotent: every event_id is recorded in stripe_event_ids; replays
    return 200 with already_processed=True (no side effects).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook_signature(payload, sig_header)
    except StripeConnectError as e:
        logger.warning(f"Invalid webhook: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type", "")
    event_id = event.get("id", "?")
    logger.info(f"Stripe webhook: {event_type} (id={event_id})")

    # Idempotency check — replays are no-ops
    from app.subscription_service import (
        record_event_or_skip,
        handle_checkout_completed,
        handle_subscription_event,
        handle_invoice_payment_succeeded,
    )
    if not record_event_or_skip(event, db):
        logger.info(f"Replay of event {event_id} ({event_type}) — skipped")
        return {"received": True, "already_processed": True, "event_id": event_id}

    # ── Subscription events ─────────────────────────────────────────────
    if event_type == "checkout.session.completed":
        # Issue #18: wrap blocking Stripe SDK call in threadpool so the event
        # loop is not stalled while handle_checkout_completed runs (it calls
        # stripe.Customer.retrieve / stripe.Subscription.retrieve synchronously).
        result = await run_in_threadpool(handle_checkout_completed, event, db)
        return {"received": True, "event_id": event_id, **result}

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        result = handle_subscription_event(event, db)
        return {"received": True, "event_id": event_id, **result}

    # ── Invoice events (WIS-660: referral payout accrual) ───────────────
    if event_type == "invoice.payment_succeeded":
        result = handle_invoice_payment_succeeded(event, db)
        return {"received": True, "event_id": event_id, **result}

    # ── Connect events (creator payouts — existing behavior) ───────────
    if event_type == "account.updated":
        data = event["data"]["object"]
        account_id = data["id"]
        user = db.query(User).filter(User.stripe_connect_id == account_id).first()
        if user:
            logger.info(f"Stripe account updated for user {user.id}: "
                       f"charges_enabled={data.get('charges_enabled')}, "
                       f"payouts_enabled={data.get('payouts_enabled')}")

    elif event_type == "transfer.failed":
        data = event["data"]["object"]
        transfer_id = data["id"]
        payout = db.query(CreatorPayout).filter(
            CreatorPayout.stripe_transfer_id == transfer_id,
        ).first()
        if payout:
            payout.status = "failed"
            db.commit()
            logger.warning(f"Transfer {transfer_id} failed for payout {payout.id}")

    elif event_type == "transfer.paid":
        data = event["data"]["object"]
        transfer_id = data["id"]
        payout = db.query(CreatorPayout).filter(
            CreatorPayout.stripe_transfer_id == transfer_id,
        ).first()
        if payout:
            payout.status = "paid"
            payout.paid_at = datetime.now(timezone.utc)
            db.commit()
            logger.info(f"Transfer {transfer_id} confirmed for payout {payout.id}")

    return {"received": True}
