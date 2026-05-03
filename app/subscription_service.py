"""Stripe subscription service for Recipes by WiseChef.

Handles:
- Customer creation (one per user, lazy-created on first checkout)
- Subscription Checkout Sessions (Cook/Operator/Studio tiers)
- Webhook event processing (subscription lifecycle)
- Idempotent webhook deduplication

API version pinned to 2026-01-28.clover (set globally via stripe.api_version).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import stripe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.discord_bot.role_sync import sync_role_for_user
from app.models import StripeEventId, User

logger = logging.getLogger(__name__)

# Pin API version (per WIS-569 risk mitigation, matches rescue product 2026-04-28)
stripe.api_version = "2026-01-28.clover"
stripe.api_key = settings.STRIPE_SECRET_KEY


# ── Tier → Price ID mapping (loaded from settings) ───────────────────────

TIER_PRICE_IDS: dict[str, str] = {
    "cook": settings.STRIPE_PRICE_COOK,
    "operator": settings.STRIPE_PRICE_OPERATOR,
    "studio": settings.STRIPE_PRICE_STUDIO,
}


class SubscriptionError(Exception):
    """Raised when a subscription operation fails."""


# ── Customer Management ──────────────────────────────────────────────────

def get_or_create_customer(user: User, db: Session) -> str:
    """Idempotently create a Stripe Customer for the user.

    Stores stripe_customer_id on the User row on first creation.
    """
    if user.stripe_customer_id:
        return user.stripe_customer_id

    if not settings.STRIPE_SECRET_KEY:
        raise SubscriptionError("Stripe not configured")

    customer = stripe.Customer.create(
        email=user.email or None,
        name=user.display_name or None,
        metadata={
            "wiserecipes_user_id": str(user.id),
            "platform": "wiserecipes",
        },
    )
    user.stripe_customer_id = customer["id"]
    db.commit()
    db.refresh(user)
    logger.info("Created Stripe customer %s for user %s", customer["id"], user.id)
    return customer["id"]


# ── Checkout Session Creation ────────────────────────────────────────────

def create_checkout_session(
    user: User,
    tier: str,
    db: Session,
    success_url: str | None = None,
    cancel_url: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session for a subscription tier.

    Returns dict with session_id and url. Raises SubscriptionError on bad input.
    """
    if tier not in TIER_PRICE_IDS:
        raise SubscriptionError(f"Unknown tier: {tier!r}. Valid: {sorted(TIER_PRICE_IDS)}")

    price_id = TIER_PRICE_IDS[tier]
    if not price_id:
        raise SubscriptionError(f"No Stripe price configured for tier {tier!r}")

    customer_id = get_or_create_customer(user, db)

    base = settings.OAUTH_REDIRECT_BASE.rstrip("/") if settings.OAUTH_REDIRECT_BASE else ""
    success_url = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = cancel_url or f"{base}/pricing"

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        # EU MOSS: collect tax IDs; Stripe Tax computes VAT
        automatic_tax={"enabled": True},
        tax_id_collection={"enabled": True},
        customer_update={"address": "auto", "name": "auto"},
        billing_address_collection="required",
        allow_promotion_codes=True,
        metadata={
            "wiserecipes_user_id": str(user.id),
            "tier": tier,
        },
        subscription_data={
            "metadata": {
                "wiserecipes_user_id": str(user.id),
                "tier": tier,
            },
        },
    )
    logger.info("Created checkout session %s for user %s tier %s", session["id"], user.id, tier)
    return {"session_id": session["id"], "url": session["url"], "tier": tier}


# ── Webhook Idempotency ──────────────────────────────────────────────────

def record_event_or_skip(event: dict, db: Session) -> bool:
    """Insert event id into dedup table; return True if first time, False if replay.

    Uses a SAVEPOINT so a duplicate-event rollback does NOT roll back the caller's
    outer transaction (which may contain webhook handler state changes the caller
    still wants committed).
    """
    record = StripeEventId(
        event_id=event["id"],
        event_type=event.get("type", ""),
        livemode=event.get("livemode"),
        processed_at=datetime.now(timezone.utc),
    )
    try:
        with db.begin_nested():
            db.add(record)
        # SAVEPOINT released; outer txn still open — caller will commit on the way out
        return True
    except IntegrityError:
        # SAVEPOINT rolled back; outer txn untouched
        return False


# ── Subscription Lifecycle ───────────────────────────────────────────────

def _user_from_subscription_metadata(sub_or_session: dict, db: Session) -> User | None:
    """Resolve user via metadata.wiserecipes_user_id, falling back to customer match."""
    from uuid import UUID

    md = sub_or_session.get("metadata") or {}
    uid = md.get("wiserecipes_user_id")
    if uid:
        try:
            user_uuid = UUID(str(uid))
        except (ValueError, TypeError):
            user_uuid = None
        if user_uuid is not None:
            user = db.query(User).filter(User.id == user_uuid).first()
            if user:
                return user
    customer_id = sub_or_session.get("customer")
    if customer_id:
        return db.query(User).filter(User.stripe_customer_id == customer_id).first()
    return None


def _apply_subscription_state(user: User, sub: dict, db: Session) -> None:
    """Sync the user's subscription_* fields from a Stripe subscription dict."""
    user.subscription_id = sub["id"]
    user.subscription_status = sub.get("status")
    period_end = sub.get("current_period_end")
    if period_end:
        user.subscription_current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
    items = (sub.get("items") or {}).get("data") or []
    if items:
        price = items[0].get("price") or {}
        md = price.get("metadata") or {}
        # Tier resolution priority: price metadata.tier > price id reverse-lookup > existing
        tier = md.get("tier")
        if not tier:
            for t, pid in TIER_PRICE_IDS.items():
                if price.get("id") == pid:
                    tier = t
                    break
        if tier:
            user.subscription_tier = tier
    db.commit()


def _maybe_sync_discord_role(user: User) -> None:
    """Best-effort role sync. No-op if Discord client isn't available
    (token unset, bot not running, or user hasn't linked Discord).
    """
    try:
        from app.discord_bot.client_singleton import get_role_client
        client = get_role_client()
        if client is None or not user.discord_user_id:
            return
        sync_role_for_user(user, client=client)
    except Exception as e:  # noqa: BLE001
        logger.warning("Discord role sync failed for user %s: %s", user.id, e)


def handle_checkout_completed(event: dict, db: Session) -> dict:
    """Handle checkout.session.completed event.

    Sets the user's subscription_status to active when the session is paid.
    """
    session = event["data"]["object"]
    if session.get("mode") != "subscription":
        return {"skipped": "non-subscription session"}
    if session.get("payment_status") != "paid":
        return {"skipped": f"payment_status={session.get('payment_status')}"}

    user = _user_from_subscription_metadata(session, db)
    if not user:
        logger.warning("No user found for checkout session %s", session["id"])
        return {"skipped": "user-not-found"}

    sub_id = session.get("subscription")
    if sub_id:
        sub = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
        _apply_subscription_state(user, dict(sub), db)
    else:
        user.subscription_status = "active"
        db.commit()
    _maybe_sync_discord_role(user)
    logger.info("Subscription activated for user %s via checkout %s", user.id, session["id"])
    return {"processed": "checkout.session.completed", "user_id": str(user.id)}


def handle_subscription_event(event: dict, db: Session) -> dict:
    """Handle customer.subscription.* events (created, updated, deleted)."""
    sub = event["data"]["object"]
    user = _user_from_subscription_metadata(sub, db)
    if not user:
        logger.warning("No user found for subscription %s", sub.get("id"))
        return {"skipped": "user-not-found"}

    event_type = event.get("type", "")
    if event_type == "customer.subscription.deleted":
        user.subscription_status = "canceled"
        user.subscription_id = None
        user.subscription_tier = None
        user.subscription_current_period_end = None
        db.commit()
        _maybe_sync_discord_role(user)
        logger.info("Subscription canceled for user %s", user.id)
        return {"processed": event_type, "user_id": str(user.id)}

    _apply_subscription_state(user, sub, db)
    _maybe_sync_discord_role(user)
    logger.info("Subscription %s for user %s: status=%s tier=%s",
                event_type, user.id, user.subscription_status, user.subscription_tier)
    return {"processed": event_type, "user_id": str(user.id)}


# ── Webhook Signature Verification ───────────────────────────────────────

def verify_subscription_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify a webhook intended for the subscription endpoint.

    Uses the SUBSCRIPTION webhook secret (separate from Connect webhook secret
    if you decide to split endpoints — currently both share STRIPE_WEBHOOK_SECRET).
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise SubscriptionError("STRIPE_WEBHOOK_SECRET not configured")
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError as e:
        raise SubscriptionError(f"Invalid signature: {e}") from e
    except Exception as e:
        raise SubscriptionError(f"Webhook verification failed: {e}") from e
