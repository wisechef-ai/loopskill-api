"""Admin routes — master-key gated operations.

POST /api/admin/reindex-all — catastrophic BM25 recovery, reindexes all skills.
GET  /api/admin/skill-publish-requests/{id}/tarball — return raw tarball BYTEA
     for a skill publish request (admin review only).
PATCH /api/admin/skill-publish-requests/{id}/status — approve or reject a
     pending skill-publish request; approval triggers a contributor-discount
     credit grant for qualifying (pro/pro_plus) authors.
GET  /api/admin/pulse — the north-star "one number" demand scoreboard:
     paying operators, MRR, free-sync paywall pressure, fleet-deploy usage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.search_index import reindex_all
from app.tier_labels import _is_paid_tier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── North-star pulse ─────────────────────────────────────────────────────────
# Monthly price per canonical tier in USD. SSOT is config/tiers.yaml
# (price_usd); duplicated here as a small constant map so the pulse query has
# no YAML I/O on the hot path. Keep in sync with tiers.yaml on any price change.
# Legacy slugs (cook->pro, operator->pro_plus) are normalised before lookup.
_TIER_MRR_USD: dict[str, int] = {"pro": 20, "pro_plus": 100}
_LEGACY_TIER_TO_CANONICAL: dict[str, str] = {"cook": "pro", "operator": "pro_plus", "studio": "pro_plus"}
# Subscription statuses that count as live, paying revenue.
_HEALTHY_SUB_STATUSES: frozenset[str] = frozenset({"active", "trialing"})


def _monthly_cents_from_stripe_sub(sub: dict) -> int:
    """Real monthly cash (in cents) a Stripe subscription bills, net of discount.

    Pure function — no network. Takes a Stripe Subscription dict (as returned by
    Subscription.list with items.data.price expanded and discount expanded) and
    returns the actual recurring cash per month after any coupon.

    This is the fix for the promo-code illusion: a 100%-off coupon makes the
    list-price irrelevant — the customer pays $0, and this returns 0. We never
    infer revenue from tier list-price again; we read what Stripe actually bills.

    Rules:
      - Sum each line item: unit_amount * quantity.
      - Normalise yearly prices to monthly (annual / 12).
      - Apply a subscription-level coupon: percent_off (×(1-pct/100)) OR
        amount_off (subtract, in the coupon's currency minor unit).
      - Clamp at 0 (a discount can't make Stripe pay the customer).
    Unknown/missing fields are treated conservatively as "no charge for that
    item" so we under-count rather than re-introduce phantom revenue.
    """
    gross = 0.0
    items = ((sub.get("items") or {}).get("data")) or []
    for it in items:
        price = it.get("price") or {}
        unit = price.get("unit_amount")
        if unit is None:
            continue  # metered/unknown price → contributes no fixed cash
        qty = it.get("quantity", 1) or 1
        recurring = price.get("recurring") or {}
        interval = recurring.get("interval", "month")
        interval_count = recurring.get("interval_count", 1) or 1
        amount = float(unit) * float(qty)
        # Normalise to a monthly figure.
        if interval == "year":
            amount /= 12.0 * interval_count
        elif interval == "week":
            amount *= 52.0 / 12.0 / interval_count
        elif interval == "day":
            amount *= 365.0 / 12.0 / interval_count
        elif interval == "month":
            amount /= float(interval_count)
        gross += amount

    # Subscription-level coupon (the promo-code path). Stripe attaches the
    # checkout coupon to the subscription's `discount.coupon`.
    discount = sub.get("discount") or {}
    coupon = (discount.get("coupon") if isinstance(discount, dict) else None) or {}
    pct = coupon.get("percent_off")
    amt = coupon.get("amount_off")
    if pct is not None:
        gross *= max(0.0, 1.0 - float(pct) / 100.0)
    elif amt is not None:
        gross -= float(amt)

    return max(0, int(round(gross)))


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


# ── North-star demand pulse ──────────────────────────────────────────────────


class PulseOut(BaseModel):
    """The 'one number' demand scoreboard for the khaserto GTM loop.

    Distinct from GET /api/stats (supply-side vanity: skill + install counts).
    This is DEMAND-side truth. The headline number is REAL CASH MRR — what
    Stripe actually bills, net of promo-code discounts — not list-price ×
    subscriber count. A 100%-off-coupon "customer" pays $0 and counts as $0.
    Master-key only.
    """

    # ── The honest headline ──────────────────────────────────────────────
    paying_operators: int  # subs whose REAL monthly cash > $0 (the NORTH STAR)
    real_cash_mrr_usd: int | None  # actual billed $/mo net of discounts; None if Stripe unreachable
    comped_subscriptions: int  # active subs paying $0 (promo/100%-off) — the illusion exposed
    mrr_source: str  # "stripe" (real) | "stripe_unavailable" (could not verify)
    # ── Context (DB-only, always available) ──────────────────────────────
    active_subscriptions: int  # all healthy paid-tier subs regardless of what they pay
    by_tier: dict[str, int]  # canonical paid tier -> count of active subscribers
    list_mrr_ceiling_usd: int  # list-price × active subs — a CEILING, NOT revenue (labeled honestly)
    # ── Paywall pressure + fleet deploy ──────────────────────────────────
    free_sync_used_total: int  # free users who burned their one free sync (felt the wall)
    free_sync_used_7d: int  # ...in the last 7 days (recent paywall pressure)
    fleets_total: int  # named fleets created
    fleet_subscriptions_total: int  # cookbook->fleet deploys (the moat motion; 0 = never used)
    fleet_subscriptions_7d: int  # ...in the last 7 days
    generated_at: str


@router.get("/pulse", response_model=PulseOut)
def admin_pulse(
    request: Request,
    db: Session = Depends(get_db),
):
    """Return the north-star demand scoreboard. Master-key only.

    Headline = REAL CASH MRR from Stripe (net of promo discounts), because our
    DB stores only tier+status, not what a customer pays — a 100%-off promo
    sub looks identical to a full-price one locally. We resolve the truth from
    Stripe per active subscriber. If Stripe is unreachable the cash figures are
    returned as None with mrr_source="stripe_unavailable" — we NEVER fall back
    to list-price as if it were revenue (that was the original bug).
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")

    from app.models import Fleet, FleetSubscription, User

    now = datetime.now(UTC)
    cutoff_7d = now - timedelta(days=7)

    # Active paid-tier subscribers (DB truth: who has a live paid tier).
    active_users = (
        db.query(User.id, User.subscription_tier, User.stripe_customer_id)
        .filter(
            User.subscription_status.in_(_HEALTHY_SUB_STATUSES),
            User.subscription_tier.isnot(None),
        )
        .all()
    )
    by_tier: dict[str, int] = {}
    active_subscriptions = 0
    list_mrr_ceiling_usd = 0
    for _uid, tier_slug, _cust in active_users:
        if not tier_slug or not _is_paid_tier(tier_slug):
            continue
        slug: str = tier_slug
        canonical = _LEGACY_TIER_TO_CANONICAL.get(slug, slug)
        by_tier[canonical] = by_tier.get(canonical, 0) + 1
        active_subscriptions += 1
        list_mrr_ceiling_usd += _TIER_MRR_USD.get(canonical, 0)

    # ── Real cash MRR from Stripe (the source of truth for what's billed) ──
    real_cash_cents = 0
    paying_operators = 0
    mrr_source = "stripe"
    customer_ids = [c for (_u, _t, c) in active_users if c]
    if not customer_ids:
        # No Stripe customers at all → unambiguously $0 real cash, no API needed.
        real_cash_mrr_usd: int | None = 0
    else:
        try:
            import stripe

            stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe.api_version = "2026-01-28.clover"
            for cust_id in customer_ids:
                subs = stripe.Subscription.list(
                    customer=cust_id,
                    status="active",
                    limit=5,
                    expand=["data.items.data.price", "data.discount"],
                )
                sub_list = getattr(subs, "data", None) or []
                for sub in sub_list:
                    cents = _monthly_cents_from_stripe_sub(dict(sub))
                    real_cash_cents += cents
                    if cents > 0:
                        paying_operators += 1
            real_cash_mrr_usd = int(round(real_cash_cents / 100.0))
        # Rationale: Stripe is the revenue source of truth; if it's unreachable we
        # report None + a flag rather than inventing revenue from list-price.
        except Exception:  # noqa: BLE001
            logger.warning("admin pulse: Stripe MRR resolution failed — reporting unavailable", exc_info=True)
            real_cash_mrr_usd = None
            paying_operators = 0
            mrr_source = "stripe_unavailable"

    comped_subscriptions = active_subscriptions - paying_operators if mrr_source == "stripe" else 0

    # Free-sync paywall pressure.
    free_sync_used_total = (
        db.query(func.count(User.id)).filter(User.free_sync_used_at.isnot(None)).scalar() or 0
    )
    free_sync_used_7d = (
        db.query(func.count(User.id)).filter(User.free_sync_used_at >= cutoff_7d).scalar() or 0
    )

    # Fleet-deploy activity (the moat motion).
    fleets_total = db.query(func.count(Fleet.id)).scalar() or 0
    fleet_subscriptions_total = db.query(func.count()).select_from(FleetSubscription).scalar() or 0
    fleet_subscriptions_7d = (
        db.query(func.count())
        .select_from(FleetSubscription)
        .filter(FleetSubscription.subscribed_at >= cutoff_7d)
        .scalar()
        or 0
    )

    logger.info(
        "admin pulse: paying=%d real_cash_mrr=%s comped=%d active=%d fleet_subs=%d source=%s",
        paying_operators,
        real_cash_mrr_usd,
        comped_subscriptions,
        active_subscriptions,
        fleet_subscriptions_total,
        mrr_source,
    )

    return PulseOut(
        paying_operators=paying_operators,
        real_cash_mrr_usd=real_cash_mrr_usd,
        comped_subscriptions=int(comped_subscriptions),
        mrr_source=mrr_source,
        active_subscriptions=active_subscriptions,
        by_tier=by_tier,
        list_mrr_ceiling_usd=list_mrr_ceiling_usd,
        free_sync_used_total=int(free_sync_used_total),
        free_sync_used_7d=int(free_sync_used_7d),
        fleets_total=int(fleets_total),
        fleet_subscriptions_total=int(fleet_subscriptions_total),
        fleet_subscriptions_7d=int(fleet_subscriptions_7d),
        generated_at=now.isoformat(),
    )
