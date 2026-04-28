"""Payout engine — monthly usage-attributed creator payout calculation.

Per recipes-plan-v4-locked.md:
- Monthly cron on 1st of each month
- Computes: installs x subscription_attribution_ratio per creator
- Writes creator_payouts rows
- Triggers Stripe transfers for creators with connected accounts

Rate tiers (locked-in for life):
  Cook:    50%
  Operator: 60%
  Studio private skills: 70%
  Recipe Bundles: 70%
  First-50 publishers (is_founder=True): 75%
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Creator, CreatorPayout, InstallEvent, Skill, User
from app.stripe_service import create_transfer, StripeConnectError

logger = logging.getLogger(__name__)

# Tier payout rates
TIER_RATES = {
    "cook": settings.PAYOUT_RATE_COOK,
    "operator": settings.PAYOUT_RATE_OPERATOR,
    "studio": settings.PAYOUT_RATE_STUDIO_PRIVATE,
    "recipe_bundle": settings.PAYOUT_RATE_RECIPE_BUNDLE,
}


def get_creator_payout_rate(skill: Skill, creator: Creator) -> float:
    """Get the payout rate for a skill based on tier and founder status."""
    if creator.is_founder:
        return settings.PAYOUT_RATE_FOUNDER_BONUS

    tier = (skill.tier or "cook").lower()
    return TIER_RATES.get(tier, settings.PAYOUT_RATE_COOK)


def compute_monthly_payouts(
    db: Session,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Compute and optionally execute monthly creator payouts.

    Args:
        db: Database session
        period_start: Start of the payout period (default: 1st of last month)
        period_end: End of the payout period (default: end of last month)
        dry_run: If True, compute but don't write to DB or trigger transfers

    Returns:
        List of payout records created/computed
    """
    now = datetime.now(timezone.utc)

    # Default period: previous calendar month
    if not period_start:
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_start = (first_of_this_month - timedelta(days=1)).replace(day=1)
    if not period_end:
        # End of period_start's month
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1)

    logger.info(f"Computing payouts for period {period_start.date()} to {period_end.date()}")

    # Aggregate installs per skill with creator info
    # Using raw SQL for efficiency with the aggregation
    query = text("""
        SELECT
            c.id AS creator_id,
            u.id AS user_id,
            u.display_name,
            u.stripe_connect_id,
            c.is_founder,
            s.id AS skill_id,
            s.slug AS skill_slug,
            s.tier AS skill_tier,
            COUNT(ie.id) AS install_count
        FROM install_events ie
        JOIN skills s ON s.id = ie.skill_id
        JOIN creators c ON c.id = s.creator_id
        JOIN users u ON u.id = c.user_id
        WHERE ie.created_at >= :period_start
          AND ie.created_at < :period_end
        GROUP BY c.id, u.id, u.display_name, u.stripe_connect_id,
                 c.is_founder, s.id, s.slug, s.tier
        ORDER BY c.id, install_count DESC
    """)

    results = db.execute(query, {
        "period_start": period_start,
        "period_end": period_end,
    }).fetchall()

    if not results:
        logger.info("No installs found for this period — nothing to pay out")
        return []

    # Group by creator and compute totals
    creator_totals: dict[str, dict] = {}
    for row in results:
        creator_id = str(row.creator_id)
        if creator_id not in creator_totals:
            creator_totals[creator_id] = {
                "creator_id": row.creator_id,
                "user_id": row.user_id,
                "display_name": row.display_name,
                "stripe_connect_id": row.stripe_connect_id,
                "is_founder": row.is_founder,
                "skills": [],
                "total_installs": 0,
                "total_gross_cents": 0,
                "total_creator_share_cents": 0,
            }

        rate = TIER_RATES.get((row.skill_tier or "cook").lower(), settings.PAYOUT_RATE_COOK)
        if row.is_founder:
            rate = settings.PAYOUT_RATE_FOUNDER_BONUS

        # Gross revenue attribution: each install attributed a share of subscription revenue
        # For now: flat rate per install (€2.00 per install as placeholder)
        # TODO: wire real subscription_attribution_ratio when billing is live
        REVENUE_PER_INSTALL_CENTS = 200  # €2.00

        gross_cents = row.install_count * REVENUE_PER_INSTALL_CENTS
        creator_share_cents = round(gross_cents * rate)

        creator_totals[creator_id]["skills"].append({
            "skill_id": str(row.skill_id),
            "slug": row.skill_slug,
            "tier": row.skill_tier,
            "installs": row.install_count,
            "rate": rate,
            "gross_cents": gross_cents,
            "creator_share_cents": creator_share_cents,
        })
        creator_totals[creator_id]["total_installs"] += row.install_count
        creator_totals[creator_id]["total_gross_cents"] += gross_cents
        creator_totals[creator_id]["total_creator_share_cents"] += creator_share_cents

    # Create payout records
    payouts = []
    for creator_id, data in creator_totals.items():
        if data["total_creator_share_cents"] < 100:
            # Skip sub-€1 payouts (Stripe minimum)
            logger.info(f"Skipping payout for {data['display_name']}: {data['total_creator_share_cents']} cents below minimum")
            continue

        payout_record = {
            "creator_id": data["user_id"],
            "creator_name": data["display_name"],
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "installs_count": data["total_installs"],
            "gross_revenue_cents": data["total_gross_cents"],
            "creator_share_cents": data["total_creator_share_cents"],
            "currency": "eur",
            "stripe_connect_id": data["stripe_connect_id"],
            "skills": data["skills"],
        }

        if not dry_run:
            # Write to DB
            payout = CreatorPayout(
                id=uuid4(),
                creator_id=data["user_id"],
                period_start=period_start,
                period_end=period_end,
                installs_count=data["total_installs"],
                gross_revenue_cents=data["total_gross_cents"],
                creator_share_cents=data["total_creator_share_cents"],
                currency="eur",
                status="pending",
            )
            db.add(payout)

            # Trigger Stripe transfer if connected
            if data["stripe_connect_id"]:
                try:
                    transfer = create_transfer(
                        account_id=data["stripe_connect_id"],
                        amount_cents=data["total_creator_share_cents"],
                        currency="eur",
                        description=f"WiseRecipes payout: {period_start.strftime('%b %Y')} ({data['total_installs']} installs)",
                        metadata={
                            "creator_id": str(data["creator_id"]),
                            "user_id": str(data["user_id"]),
                            "period": period_start.strftime("%Y-%m"),
                            "installs": str(data["total_installs"]),
                        },
                        transfer_group=f"wr-payout-{period_start.strftime('%Y-%m')}",
                    )
                    if transfer:
                        payout.stripe_transfer_id = transfer.id
                        payout.status = "paid"
                        payout.paid_at = datetime.now(timezone.utc)
                        payout_record["stripe_transfer_id"] = transfer.id
                        payout_record["status"] = "paid"
                        logger.info(f"Transfer {transfer.id} for {data['display_name']}: {data['total_creator_share_cents']} cents")
                except StripeConnectError as e:
                    logger.error(f"Transfer failed for {data['display_name']}: {e}")
                    payout.status = "failed"
                    payout_record["status"] = "failed"
            else:
                payout_record["status"] = "pending_no_stripe"

            db.commit()
            db.refresh(payout)
            payout_record["id"] = str(payout.id)
        else:
            payout_record["status"] = "dry_run"
            payout_record["id"] = None

        payouts.append(payout_record)

    logger.info(f"Payout computation complete: {len(payouts)} payouts, "
                f"{sum(p['creator_share_cents'] for p in payouts)} total cents")
    return payouts


def get_creator_earnings(db: Session, user_id) -> dict:
    """Get earnings summary for a creator."""
    from uuid import UUID

    # Total payouts
    totals = db.query(
        func.coalesce(func.sum(CreatorPayout.installs_count), 0).label("total_installs"),
        func.coalesce(func.sum(CreatorPayout.gross_revenue_cents), 0).label("total_gross"),
        func.coalesce(func.sum(CreatorPayout.creator_share_cents), 0).label("total_earned"),
        func.count(CreatorPayout.id).label("payout_count"),
    ).filter(
        CreatorPayout.creator_id == user_id,
        CreatorPayout.status.in_(["pending", "paid"]),
    ).first()

    # Pending amount
    pending = db.query(
        func.coalesce(func.sum(CreatorPayout.creator_share_cents), 0),
    ).filter(
        CreatorPayout.creator_id == user_id,
        CreatorPayout.status == "pending",
    ).scalar()

    # Paid amount
    paid = db.query(
        func.coalesce(func.sum(CreatorPayout.creator_share_cents), 0),
    ).filter(
        CreatorPayout.creator_id == user_id,
        CreatorPayout.status == "paid",
    ).scalar()

    # This month's installs (for live dashboard)
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_installs = db.query(func.count(InstallEvent.id)).join(
        Skill, Skill.id == InstallEvent.skill_id
    ).join(
        Creator, Creator.id == Skill.creator_id
    ).filter(
        Creator.user_id == user_id,
        InstallEvent.created_at >= month_start,
    ).scalar()

    return {
        "total_installs": totals.total_installs,
        "total_gross_cents": totals.total_gross,
        "total_earned_cents": totals.total_earned,
        "total_payouts": totals.payout_count,
        "pending_cents": pending,
        "paid_cents": paid,
        "this_month_installs": month_installs,
    }
