"""app/subscriber_credit_service.py — contributor-discount credit service.

Implements the three core operations for the contributor-discount system:

  grant_contributor_credit(db, user_id, skill_id)
      Called from the skill-approval hook in admin_routes.  Grants a 50%
      discount credit to the skill author when their skill is approved, but
      only if they are a pro/pro_plus subscriber and have no existing unused
      credit.  Idempotent: a second call for the same (user, skill) pair
      returns the existing credit.

  apply_credit_to_stripe_coupon(db, credit_id)
      Creates a one-time Stripe Coupon with percent_off = credit.amount_pct.
      Returns the Stripe coupon ID for use in invoice modification or
      subscription update flows.

  expire_stale_credits(db)
      Marks credits whose expires_at has passed as "used" (tombstones them)
      so they no longer show up as available.  Called by the nightly cron.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Tiers that qualify for contributor discounts
_PRO_TIERS = {"pro", "pro_plus", "operator", "studio"}  # operator/studio are legacy aliases


# ---------------------------------------------------------------------------
# grant_contributor_credit
# ---------------------------------------------------------------------------


def grant_contributor_credit(
    db: Session,
    user_id: UUID,
    skill_id: UUID | None,
) -> object | None:
    """Grant a 50 % contributor-discount credit to a pro/pro_plus subscriber.

    Returns the existing or newly-created SubscriberCredit row, or None if
    the user is not on a qualifying tier.

    Guard rails (applied in order):
      1. User must be on pro or pro_plus tier (legacy aliases included).
      2. Idempotency: if a credit for (user_id, skill_id) already exists,
         return it without creating a duplicate.
      3. Deduplicate active credits: if the user already has an unused credit
         (used_at IS NULL) for a *different* skill, skip granting a new one
         and log a warning — the existing credit must be consumed first.
    """
    from app.models import SubscriberCredit, User

    # 1. Qualify tier
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        logger.warning("grant_contributor_credit: user %s not found", user_id)
        return None

    tier = (user.subscription_tier or "").lower()
    if tier not in _PRO_TIERS:
        logger.info(
            "grant_contributor_credit: user %s tier=%r is not pro/pro_plus, skipping",
            user_id,
            tier,
        )
        return None

    # 2. Idempotency — same (user, skill) pair
    existing = (
        db.query(SubscriberCredit)
        .filter(
            SubscriberCredit.user_id == user_id,
            SubscriberCredit.granted_for_skill_id == skill_id,
        )
        .first()
    )
    if existing is not None:
        logger.info(
            "grant_contributor_credit: idempotent — credit %s already exists for " "user=%s skill=%s",
            existing.id,
            user_id,
            skill_id,
        )
        return existing

    # 3. Check for any existing unused credit (different skill)
    unused = (
        db.query(SubscriberCredit)
        .filter(
            SubscriberCredit.user_id == user_id,
            SubscriberCredit.used_at.is_(None),
        )
        .first()
    )
    if unused is not None:
        logger.warning(
            "grant_contributor_credit: user %s already has unused credit %s "
            "(granted_for=%s); skipping new grant for skill %s",
            user_id,
            unused.id,
            unused.granted_for_skill_id,
            skill_id,
        )
        return unused

    # Compute expiry: user.subscription_current_period_end + 90 days,
    # or NOW + 180 days as the safe fallback when no renewal date is stored.
    period_end = getattr(user, "subscription_current_period_end", None)
    if period_end is not None:
        expires_at = period_end + timedelta(days=90)
    else:
        expires_at = datetime.now(UTC) + timedelta(days=180)

    credit = SubscriberCredit(
        user_id=user_id,
        type="contributor_discount",
        amount_pct=50,
        granted_for_skill_id=skill_id,
        expires_at=expires_at,
    )
    db.add(credit)
    db.commit()
    db.refresh(credit)

    logger.info(
        "grant_contributor_credit: granted credit %s to user %s for skill %s " "(expires %s)",
        credit.id,
        user_id,
        skill_id,
        expires_at.isoformat(),
    )
    return credit


# ---------------------------------------------------------------------------
# apply_credit_to_stripe_coupon
# ---------------------------------------------------------------------------


def apply_credit_to_stripe_coupon(db: Session, credit_id: UUID) -> str:
    """Create a one-time Stripe Coupon for the given credit and return its ID.

    The coupon is created with:
      - percent_off = credit.amount_pct
      - max_redemptions = 1 (one-time use)
      - duration = once (applies to a single invoice)

    The caller is responsible for attaching the coupon to the Stripe
    subscription or invoice; this function only creates the coupon object.

    Raises ValueError if the credit is not found or already used.
    """
    from app.config import settings
    from app.models import SubscriberCredit

    credit = db.query(SubscriberCredit).filter(SubscriberCredit.id == credit_id).first()
    if credit is None:
        raise ValueError(f"SubscriberCredit {credit_id} not found")
    if credit.used_at is not None:
        raise ValueError(f"SubscriberCredit {credit_id} is already used")

    import stripe  # type: ignore[import]

    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = "2026-01-28.clover"

    coupon = stripe.Coupon.create(
        percent_off=credit.amount_pct,
        duration="once",
        max_redemptions=1,
        metadata={
            "subscriber_credit_id": str(credit_id),
            "user_id": str(credit.user_id),
        },
        idempotency_key=f"coupon_{credit_id}",
    )
    logger.info(
        "apply_credit_to_stripe_coupon: created Stripe coupon %s for credit %s " "(%.0f%% off)",
        coupon.id,
        credit_id,
        credit.amount_pct,
    )
    return coupon.id


# ---------------------------------------------------------------------------
# expire_stale_credits
# ---------------------------------------------------------------------------


def expire_stale_credits(db: Session) -> int:
    """Mark expired-but-unused credits as consumed (tombstone via used_at).

    Sets used_at = NOW() for all credits where:
      - expires_at < NOW()
      - used_at IS NULL

    Returns the number of credits that were expired.
    """
    from sqlalchemy import update

    from app.models import SubscriberCredit

    now = datetime.now(UTC)
    result = db.execute(
        update(SubscriberCredit)
        .where(
            SubscriberCredit.expires_at < now,
            SubscriberCredit.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.commit()
    expired_count: int = result.rowcount  # type: ignore[assignment]
    if expired_count:
        logger.info("expire_stale_credits: expired %d stale credit(s)", expired_count)
    return expired_count
