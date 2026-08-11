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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.billable_units import billable_units
from app.config import settings
from app.database import get_db
from app.revenue_truth import (
    HEALTHY_SUB_STATUSES,
    cents_to_usd,
    real_monthly_cents,
    tier_list_monthly_usd,
)
from app.search_index import reindex_all
from app.tier_labels import _is_paid_tier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── North-star pulse ─────────────────────────────────────────────────────────
# Money and the comped/real/list vocabulary come from app.revenue_truth — the
# ONE module allowed to answer "did money actually move?". This route used to
# carry its own float-based discount maths and its own hand-maintained tier
# price map (which had drifted to $20 for a $9.95 tier: a duplicated price
# constant is a lie waiting for someone to trust it). Both now live in the
# shared helper, so the pulse and the Discord revenue alerts cannot disagree.
_LEGACY_TIER_TO_CANONICAL: dict[str, str] = {"cook": "pro", "operator": "pro_plus", "studio": "pro_plus"}


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


class OrgBillableUnitsOut(BaseModel):
    """Per-tenant count of the billable-CANDIDATE unit. NOT a charge.

    LoopSkill has a cap but no meter (see :mod:`app.billable_units`). These are
    the counters a future meter would attach to — exposed now, org-scoped, with
    synthetic traffic separable, so that decision becomes a config change
    against reconciled history instead of an integration project. No price, no
    Stripe usage record, no metered SKU is implied (lock #24).
    """

    org_id: str | None  # None = personal-scope fleets (Fleet.org_id IS NULL)
    org_name: str | None
    active_fleet_members: int  # enrolled agents, EXCLUDING synthetic
    active_fleet_members_synthetic: int  # test/CI/internal keys (api_keys.is_test)
    loop_runs: int  # runs in period attributable to a non-synthetic member
    loop_runs_synthetic: int  # runs from a synthetic member (e.g. the self-beacon)
    loop_runs_unattributed: int  # member_id resolves to no member — NOT rounded down to 0


class PulseOut(BaseModel):
    """The 'one number' demand scoreboard for the khaserto GTM loop.

    Distinct from GET /api/stats (supply-side vanity: skill + install counts).
    This is DEMAND-side truth. The headline number is REAL CASH MRR — what
    Stripe actually bills, net of promo-code discounts — not list-price ×
    subscriber count. A 100%-off-coupon "customer" pays $0 and counts as $0.
    Master-key only.

    Money fields are ``Decimal`` and serialise as JSON *strings* ("9.95"), not
    floats. Exactness beats convenience for a revenue figure.
    """

    # ── The honest headline ──────────────────────────────────────────────
    paying_operators: int  # subs whose REAL monthly cash > $0 (the NORTH STAR)
    real_cash_mrr_usd: Decimal | None  # actual billed $/mo net of discounts; None if Stripe unreachable
    comped_subscriptions: int  # active subs paying $0 (promo/100%-off) — the illusion exposed
    mrr_source: str  # "stripe" (real) | "stripe_unavailable" (could not verify)
    # ── Context (DB-only, always available) ──────────────────────────────
    active_subscriptions: int  # all healthy paid-tier subs regardless of what they pay
    by_tier: dict[str, int]  # canonical paid tier -> count of active subscribers
    list_mrr_ceiling_usd: Decimal  # list-price × active subs — a CEILING, NOT revenue (labeled honestly)
    # ── Paywall pressure + fleet deploy ──────────────────────────────────
    free_sync_used_total: int  # free users who burned their one free sync (felt the wall)
    free_sync_used_7d: int  # ...in the last 7 days (recent paywall pressure)
    fleets_total: int  # named fleets created
    fleet_subscriptions_total: int  # cookbook->fleet deploys (the moat motion; 0 = never used)
    fleet_subscriptions_7d: int  # ...in the last 7 days
    # ── Billable-candidate units (the meter that doesn't exist yet) ───────
    billable_units: list[OrgBillableUnitsOut]
    billable_units_period_start: str
    billable_units_period_end: str
    # ── bundles0811-P1 (§0 lock #2) — "Success = bundle COUNT and bundle
    # AUTHORS, not skill count." Replaces skill-count-shaped vanity metrics
    # on this dashboard with the two numbers the lock names explicitly.
    bundles_total: int  # every bundle regardless of visibility (private+public)
    bundles_public_total: int  # public subset — the shareable, forkable kind
    bundle_authors_total: int  # DISTINCT bundle_owner across ALL bundles (the "AUTHORS" half of lock #2)
    generated_at: str


@router.get("/pulse", response_model=PulseOut)
def admin_pulse(
    request: Request,
    db: Session = Depends(get_db),
    org_id: UUID | None = None,
):
    """Return the north-star demand scoreboard. Master-key only.

    Headline = REAL CASH MRR from Stripe (net of promo discounts), because our
    DB stores only tier+status, not what a customer pays — a 100%-off promo
    sub looks identical to a full-price one locally. We resolve the truth from
    Stripe per active subscriber. If Stripe is unreachable the cash figures are
    returned as None with mrr_source="stripe_unavailable" — we NEVER fall back
    to list-price as if it were revenue (that was the original bug).

    ``org_id`` narrows the ``billable_units`` breakdown to one tenant (the cash
    and paywall figures are account-wide and unaffected). Omit it for every org.
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
            User.subscription_status.in_(HEALTHY_SUB_STATUSES),
            User.subscription_tier.isnot(None),
        )
        .all()
    )
    by_tier: dict[str, int] = {}
    active_subscriptions = 0
    list_ceiling = Decimal(0)
    for _uid, tier_slug, _cust in active_users:
        if not tier_slug or not _is_paid_tier(tier_slug):
            continue
        slug: str = tier_slug
        canonical = _LEGACY_TIER_TO_CANONICAL.get(slug, slug)
        by_tier[canonical] = by_tier.get(canonical, 0) + 1
        active_subscriptions += 1
        list_ceiling += tier_list_monthly_usd(canonical)

    # ── Real cash MRR from Stripe (the source of truth for what's billed) ──
    real_cash_cents = Decimal(0)
    paying_operators = 0
    mrr_source = "stripe"
    customer_ids = [c for (_u, _t, c) in active_users if c]
    if not customer_ids:
        # No Stripe customers at all → unambiguously $0 real cash, no API needed.
        real_cash_mrr_usd: Decimal | None = cents_to_usd(0)
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
                    expand=["data.items.data.price", "data.discount", "data.discounts"],
                )
                sub_list = getattr(subs, "data", None) or []
                for sub in sub_list:
                    # Exact Decimal cents, accumulated unrounded: rounding once
                    # at the end beats accumulating per-subscription error.
                    cents = real_monthly_cents(dict(sub))
                    real_cash_cents += cents
                    if cents > 0:
                        paying_operators += 1
            real_cash_mrr_usd = cents_to_usd(real_cash_cents)
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

    # Billable-candidate units per org (instrumentation only — see
    # app.billable_units; no price, no usage record, lock #24).
    units = billable_units(db, org_id=org_id)

    # bundles0811-P1 (§0 lock #2) — bundle count + distinct bundle-author
    # count. This is deliberately NOT scoped by org_id (unlike
    # billable_units above): lock #2's "success" metric is account-wide by
    # definition — a per-org filter would understate it the same way the
    # skill-count vanity metric it replaces overstated supply-side activity.
    from app.models import Bundle as _Bundle

    bundles_total = db.query(func.count(_Bundle.id)).scalar() or 0
    bundles_public_total = (
        db.query(func.count(_Bundle.id)).filter(_Bundle.visibility == "public").scalar() or 0
    )
    bundle_authors_total = (
        db.query(func.count(func.distinct(_Bundle.bundle_owner)))
        .filter(_Bundle.bundle_owner.isnot(None))
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
        list_mrr_ceiling_usd=list_ceiling.quantize(Decimal("0.01")),
        free_sync_used_total=int(free_sync_used_total),
        free_sync_used_7d=int(free_sync_used_7d),
        fleets_total=int(fleets_total),
        fleet_subscriptions_total=int(fleet_subscriptions_total),
        fleet_subscriptions_7d=int(fleet_subscriptions_7d),
        billable_units=[
            OrgBillableUnitsOut(
                org_id=str(u.org_id) if u.org_id else None,
                org_name=u.org_name,
                active_fleet_members=u.active_fleet_members,
                active_fleet_members_synthetic=u.active_fleet_members_synthetic,
                loop_runs=u.loop_runs,
                loop_runs_synthetic=u.loop_runs_synthetic,
                loop_runs_unattributed=u.loop_runs_unattributed,
            )
            for u in units.orgs
        ],
        billable_units_period_start=units.period_start.isoformat(),
        billable_units_period_end=units.period_end.isoformat(),
        bundles_total=int(bundles_total),
        bundles_public_total=int(bundles_public_total),
        bundle_authors_total=int(bundle_authors_total),
        generated_at=now.isoformat(),
    )


# ── activate_0701 Phase T — sync-report admin endpoints ──────────────────────


class RollupIn(BaseModel):
    day: date | None = None  # type: ignore[assignment]


class PruneIn(BaseModel):
    days: int = 30


@router.post("/loop-run-rollup")
def admin_loop_run_rollup(
    body: RollupIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Trigger idempotent daily rollup of LoopRun rows into LoopRunDailyRollup.

    Master-only (cron-callable). If ``day`` is omitted, defaults to today.
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")

    from app.services.sync_report import rollup_loop_runs

    target_day = body.day if body.day is not None else date.today()
    count = rollup_loop_runs(db, day=target_day)
    logger.info("admin loop-run-rollup: %d rollup rows for %s", count, target_day)
    return {"rolled_up": count, "day": target_day.isoformat()}


@router.post("/sync-report-prune")
def admin_sync_report_prune(
    body: PruneIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Prune raw sync-report telemetry past retention (30d default).

    Master-only (cron-callable). NEVER touches rollups.
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")

    from app.services.sync_report import prune_raw

    counts = prune_raw(db, older_than_days=body.days)
    logger.info("admin sync-report-prune: %s", counts)
    return {"pruned": counts}
