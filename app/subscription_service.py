"""Stripe subscription service for Recipes by WiseChef.

Handles:
- Customer creation (one per user, lazy-created on first checkout)
- Subscription Checkout Sessions (Pro/Pro+ tiers)
- Webhook event processing (subscription lifecycle)
- Idempotent webhook deduplication

API version pinned to 2026-01-28.clover (set globally via stripe.api_version).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import stripe
import yaml
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.discord_bot.role_sync import sync_role_for_user
from app.models import CreatorPayout, Referral, StripeEventId, User
from app.revenue_alerts import post_revenue_event

logger = logging.getLogger(__name__)

# Pin API version (per WIS-569 risk mitigation, matches rescue product 2026-04-28)
stripe.api_version = "2026-01-28.clover"
stripe.api_key = settings.STRIPE_SECRET_KEY

# Path to the SSOT tiers config
_TIERS_YAML = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"


# ── SSOT tier config helpers ─────────────────────────────────────────────


@lru_cache(maxsize=1)
def _load_tiers_yaml() -> dict:
    """Load and cache config/tiers.yaml."""
    with open(_TIERS_YAML) as f:
        return yaml.safe_load(f)["tiers"]


@lru_cache(maxsize=1)
def _load_tier_price_ids() -> dict[str, str]:
    """Return {db_slug: price_id} from config/tiers.yaml.

    Reads the price_id_env field for each tier and resolves via settings.
    If the canonical env var resolves to an empty string, falls back to
    price_id_env_legacy (RCP-INCIDENT-2026-05-11 Phase 6 — env var rename
    soak window, expires 2026-06-10).
    Tiers without price_id_env (free tier) are excluded.
    """
    tiers = _load_tiers_yaml()
    result: dict[str, str] = {}
    for db_slug, meta in tiers.items():
        env_name = meta.get("price_id_env")
        if not env_name:
            continue
        # settings uses WR_ prefix (pydantic-settings env_prefix="WR_")
        # so WR_STRIPE_PRICE_PRO → settings.STRIPE_PRICE_PRO
        attr = env_name.removeprefix("WR_")
        val = getattr(settings, attr, None) or ""
        if not val:
            # Fall back to the legacy env var name if the canonical is empty.
            # This lets a stale .env (still has WR_STRIPE_PRICE_COOK but not
            # WR_STRIPE_PRICE_PRO yet) keep working through the rename window.
            legacy = meta.get("price_id_env_legacy")
            if legacy:
                legacy_attr = legacy.removeprefix("WR_")
                legacy_val = getattr(settings, legacy_attr, None) or ""
                if legacy_val:
                    logger.info(
                        "Tier %r resolved Stripe price via LEGACY env var %s "
                        "(canonical %s was empty). Set the canonical var in .env "
                        "to silence this — legacy fallback removed after 2026-06-10.",
                        db_slug,
                        legacy,
                        env_name,
                    )
                    val = legacy_val
        if val:
            result[db_slug] = val
    return result


@lru_cache(maxsize=1)
def _load_tier_usd_price() -> dict[str, float]:
    """Return {db_slug: price_usd} from config/tiers.yaml."""
    tiers = _load_tiers_yaml()
    return {slug: float(meta["price_usd"]) for slug, meta in tiers.items() if "price_usd" in meta}


# ── Tier → Price ID mapping (loaded from SSOT config/tiers.yaml) ──────────

TIER_PRICE_IDS: dict[str, str] = _load_tier_price_ids()

# Display-friendly USD price for revenue alerts. Loaded from config/tiers.yaml.
# Used purely for Discord notifications — no billing logic depends on this.
# Also include legacy slugs for the 30-day backwards-compat window.
# RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
TIER_USD_PRICE: dict[str, float] = {
    **_load_tier_usd_price(),
    # Legacy aliases — pro=20, pro_plus=100
    "cook": _load_tier_usd_price().get("pro", 20.0),  # legacy alias → pro
    "operator": _load_tier_usd_price().get("pro_plus", 100.0),  # legacy alias → pro_plus
    "studio": _load_tier_usd_price().get("pro_plus", 100.0),  # legacy alias → pro_plus
}


class SubscriptionError(Exception):
    """Raised when a subscription operation fails."""


# ── Backwards-compat slug normalisation ─────────────────────────────────


def _normalise_tier(tier: str | None) -> str | None:
    """Translate legacy slugs to canonical names.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    Legacy → canonical:
      studio → pro_plus  (legacy alias from Phase 3, now rewrites through to pro_plus)
      operator → pro_plus  (legacy alias from Phase 5)
      cook → pro  (legacy alias from Phase 5)
    """
    LEGACY = {"studio": "pro_plus", "operator": "pro_plus", "cook": "pro"}  # legacy aliases
    if tier in LEGACY:
        canonical = LEGACY[tier]
        logger.warning(
            "DEPRECATION: tier slug %r received — normalising to %r. "
            "This shim will be removed after 2026-06-10 (RCP-INCIDENT-2026-05-11).",
            tier,
            canonical,
        )
        return canonical
    return tier


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
        idempotency_key=f"customer_create_{user.id}",
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
    promo_code: str | None = None,
    utm_ref: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session for a subscription tier.

    Returns dict with session_id and url. Raises SubscriptionError on bad input.

    Optional ``promo_code`` (e.g. "WELCOME50") will be looked up against
    Stripe's promotion_codes index and pre-applied to the checkout — the
    user lands on Stripe with the discount already showing instead of
    having to expand the (default-collapsed) "Add promotion code" link.

    Falsy / unknown / inactive codes are silently ignored — never raise
    on the buyer's side. Stripe's own UI still lets them type any other
    valid code in addition to or instead of this one.

    Optional ``utm_ref`` (from cookie ``recipes_utm_ref``) is attached to
    both the session-level metadata and subscription_data.metadata so Stripe
    propagates it to both the customer and subscription objects.
    """
    # Normalise legacy 'studio' slug before checking TIER_PRICE_IDS
    tier = _normalise_tier(tier) or tier

    if tier not in TIER_PRICE_IDS:
        raise SubscriptionError(f"Unknown tier: {tier!r}. Valid: {sorted(TIER_PRICE_IDS)}")

    price_id = TIER_PRICE_IDS[tier]
    if not price_id:
        raise SubscriptionError(f"No Stripe price configured for tier {tier!r}")

    customer_id = get_or_create_customer(user, db)

    base = settings.OAUTH_REDIRECT_BASE.rstrip("/") if settings.OAUTH_REDIRECT_BASE else ""
    success_url = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = cancel_url or f"{base}/pricing"

    # Pre-apply a promotion code if the caller supplied one. This is
    # additive UX over Stripe's built-in (default-collapsed) "Add
    # promotion code" link — buyers can still enter codes manually.
    discounts: list[dict[str, str]] = []
    if promo_code:
        normalized = promo_code.strip().upper()
        try:
            results = stripe.PromotionCode.list(code=normalized, active=True, limit=1)
            # stripe.ListObject behaves like a dict but ``.get`` may not be
            # available depending on SDK version — read ``data`` attribute
            # directly with fallback to dict-style access in tests.
            data = getattr(results, "data", None)
            if data is None and hasattr(results, "__getitem__"):
                try:
                    data = results["data"]
                except (KeyError, TypeError):
                    data = []
            promo_obj = (data or [None])[0]
            if promo_obj:
                # promo_obj["id"] works for both dict (tests) and stripe.PromotionCode
                promo_id = promo_obj["id"] if hasattr(promo_obj, "__getitem__") else promo_obj.id
                discounts.append({"promotion_code": promo_id})
                logger.info(
                    "Pre-applied promotion code %s (id=%s) for user %s tier %s",
                    normalized,
                    promo_id,
                    user.id,
                    tier,
                )
            else:
                logger.info(
                    "Promotion code %r not found / inactive — ignoring (user %s)",
                    normalized,
                    user.id,
                )
        # Rationale: promo code lookup failure must never block checkout; log and continue
        except Exception as e:  # noqa: BLE001 — never block checkout on a bad code
            logger.warning(
                "Promotion code lookup failed for %r (user %s): %s",
                normalized,
                user.id,
                e,
            )

    checkout_kwargs: dict[str, Any] = dict(
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
        metadata={
            "wiserecipes_user_id": str(user.id),
            "tier": tier,
            **({"utm_ref": utm_ref} if utm_ref else {}),
        },
        subscription_data={
            "metadata": {
                "wiserecipes_user_id": str(user.id),
                "tier": tier,
                **({"utm_ref": utm_ref} if utm_ref else {}),
            },
        },
    )
    # Stripe disallows allow_promotion_codes when discounts are pre-applied,
    # but we still want users to be able to enter other codes when none
    # was pre-applied. Set the flags conditionally.
    if discounts:
        checkout_kwargs["discounts"] = discounts
    else:
        checkout_kwargs["allow_promotion_codes"] = True

    # Deterministic idempotency key: same user + price → same checkout request.
    # Stripe deduplicates concurrent retries within a 24-hour window, so a
    # hammering frontend or transient network retry produces exactly one session.
    checkout_kwargs["idempotency_key"] = f"checkout_{user.id}_{price_id}"

    session = stripe.checkout.Session.create(**checkout_kwargs)
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
        processed_at=datetime.now(UTC),
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
        user.subscription_current_period_end = datetime.fromtimestamp(period_end, tz=UTC)
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
            # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
            tier = _normalise_tier(tier) or tier
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
    # Rationale: Discord role sync is best-effort; failure must never block subscription update
    except Exception as e:  # noqa: BLE001
        logger.warning("Discord role sync failed for user %s: %s", user.id, e)


def _handle_founding_completed(session: dict, db: Session) -> dict:
    """Grant a Founding Integrator seat from a paid one-time checkout session.

    loopclose_3005 Phase D. Replay-safe (grant_founding_membership is idempotent
    on already-seated users). On a sold-out race (the unique slot lost), the
    one-time PaymentIntent is refunded automatically — the premortem F8.1 term
    promises deploy-or-refund, so an over-sell refund is an expected branch.
    """
    if session.get("payment_status") != "paid":
        return {"skipped": f"payment_status={session.get('payment_status')}"}

    user = _user_from_subscription_metadata(session, db)
    if not user:
        logger.warning("No user found for founding session %s", session.get("id"))
        return {"skipped": "user-not-found"}

    from app.founding_service import (
        FoundingSoldOutError,
        founding_price_usd,
        grant_founding_membership,
    )

    try:
        result = grant_founding_membership(user, db)
    except FoundingSoldOutError:
        # Lost the race for the last seat — refund the one-time charge so the
        # buyer is never charged for a seat they can't have.
        _refund_founding_payment(session, user)
        return {"founding": "sold_out_refunded", "user_id": str(user.id)}

    _maybe_sync_discord_role(user)

    # Revenue alert on a genuinely new grant only (skip replays).
    if result.get("granted"):
        try:
            price_usd = None
            try:
                price_usd = founding_price_usd()
            # Rationale: price lookup is display-only; never block the grant alert
            except Exception:  # noqa: BLE001
                price_usd = None
            post_revenue_event(
                event_kind="new_subscription",
                user_email=user.email,
                user_id=str(user.id),
                tier=f"founding (#{result.get('slot_number')})",
                amount_usd=price_usd,
                extra_lines=[
                    f"Founding Integrator seat #{result.get('slot_number')}",
                    f"Stripe checkout: `{session.get('id', '?')}`",
                ],
            )
        # Rationale: revenue alert dispatch must never block the founding webhook
        except Exception:  # noqa: BLE001
            logger.exception("revenue_alerts: founding dispatch failed")

    logger.info(
        "Founding seat processed for user %s via checkout %s (granted=%s, seat=%s)",
        user.id,
        session.get("id"),
        result.get("granted"),
        result.get("slot_number"),
    )
    return {"processed": "founding", "user_id": str(user.id), **result}


def _refund_founding_payment(session: dict, user: User) -> None:
    """Best-effort refund of a founding one-time payment (sold-out race).

    Looks up the session's payment_intent and issues a full refund. Never
    raises — a refund failure is logged for manual follow-up but must not
    crash the webhook (which would make Stripe retry and re-refund).
    """
    payment_intent = session.get("payment_intent")
    if not payment_intent:
        logger.error(
            "Founding sold-out refund: no payment_intent on session %s (user %s) — MANUAL REFUND NEEDED",
            session.get("id"),
            user.id,
        )
        return
    try:
        stripe.Refund.create(
            payment_intent=payment_intent,
            reason="requested_by_customer",
            idempotency_key=f"founding_soldout_refund_{payment_intent}",
        )
        logger.info(
            "Refunded founding over-sell for user %s (payment_intent=%s)",
            user.id,
            payment_intent,
        )
    # Rationale: refund failure must not crash the webhook; log for manual action
    except Exception:  # noqa: BLE001
        logger.exception(
            "Founding sold-out refund FAILED for user %s (payment_intent=%s) — MANUAL REFUND NEEDED",
            user.id,
            payment_intent,
        )


def handle_checkout_completed(event: dict, db: Session) -> dict:
    """Handle checkout.session.completed event.

    Routes by session mode:
    - mode=subscription → activate the recurring subscription (Pro / Pro+)
    - mode=payment + metadata.kind=founding → grant lifetime founding membership
      (loopclose_3005 Phase D — one-time Founding Integrator SKU)

    Any other one-time payment session is ignored (skipped).
    """
    session = event["data"]["object"]

    # ── Founding Integrator (one-time payment) ──────────────────────────
    md = session.get("metadata") or {}
    if session.get("mode") == "payment" and md.get("kind") == "founding":
        return _handle_founding_completed(session, db)

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

    # Revenue alert: ping Discord on first paid signup. Internal users
    # (chef@/tori@/adam.krawczyk0698 on $0 Co-worker price) still trigger
    # this — that's intentional, the team should see every checkout completion
    # to confirm the pipe is alive.
    try:
        post_revenue_event(
            event_kind="new_subscription",
            user_email=user.email,
            user_id=str(user.id),
            tier=user.subscription_tier,
            amount_usd=TIER_USD_PRICE.get((user.subscription_tier or "").lower()),
            extra_lines=[
                f"Stripe checkout: `{session.get('id', '?')}`",
                f"Stripe subscription: `{sub_id or '(none)'}`",
            ],
        )
    # Rationale: revenue alert dispatch must never block the checkout webhook handler
    except Exception:  # noqa: BLE001 — never block the webhook on alerting
        logger.exception("revenue_alerts: new_subscription dispatch failed")

    return {"processed": "checkout.session.completed", "user_id": str(user.id)}


def handle_subscription_event(event: dict, db: Session) -> dict:
    """Handle customer.subscription.* events (created, updated, deleted).

    On `customer.subscription.deleted`, the user is downgraded to free tier
    (subscription_tier=None, subscription_status="canceled"). Their installed
    skills keep working locally; only auto-improvement updates and new catalog
    access stop.
    """
    sub = event["data"]["object"]
    user = _user_from_subscription_metadata(sub, db)
    if not user:
        logger.warning("No user found for subscription %s", sub.get("id"))
        return {"skipped": "user-not-found"}

    event_type = event.get("type", "")
    prior_tier = (user.subscription_tier or "").lower() if user.subscription_tier else None

    if event_type == "customer.subscription.deleted":
        # Cancel → downgrade-to-free: clear sub fields so the user is back on Free.
        user.subscription_status = "canceled"
        user.subscription_id = None
        user.subscription_tier = None
        user.subscription_current_period_end = None
        db.commit()
        _maybe_sync_discord_role(user)
        logger.info("Subscription canceled for user %s — downgraded to free", user.id)

        try:
            post_revenue_event(
                event_kind="subscription_canceled",
                user_email=user.email,
                user_id=str(user.id),
                tier=prior_tier,
                amount_usd=TIER_USD_PRICE.get(prior_tier or ""),
                extra_lines=[
                    f"Stripe subscription: `{sub.get('id', '?')}`",
                    "Installed skills keep working locally; only auto-improvement updates stop.",
                ],
            )
        # Rationale: cancel revenue alert must never block the subscription-cancel webhook path
        except Exception:  # noqa: BLE001
            logger.exception("revenue_alerts: cancel dispatch failed")
        return {"processed": event_type, "user_id": str(user.id), "downgraded_to": "free"}

    _apply_subscription_state(user, sub, db)
    # marketing_1205: persist utm_ref from subscription metadata (or session
    # metadata fallback) onto the user row so attribution survives the webhook.
    sub_meta = sub.get("metadata") or {}
    utm_ref = sub_meta.get("utm_ref")
    if utm_ref and not user.utm_ref:
        user.utm_ref = utm_ref[:32]
        db.commit()
    _maybe_sync_discord_role(user)
    logger.info(
        "Subscription %s for user %s: status=%s tier=%s",
        event_type,
        user.id,
        user.subscription_status,
        user.subscription_tier,
    )

    # Tier-change alert: only fire when the tier actually moved (Pro→Pro+ etc).
    # Skip for noise events (status: past_due → active churn-recovery, billing
    # period rollovers, payment-method updates) where the tier is unchanged.
    new_tier = (user.subscription_tier or "").lower() if user.subscription_tier else None
    if new_tier and new_tier != prior_tier:
        try:
            kind = "subscription_upgrade"
            if prior_tier and TIER_USD_PRICE.get(new_tier, 0) < TIER_USD_PRICE.get(prior_tier, 0):
                kind = "subscription_downgrade"
            post_revenue_event(
                event_kind=kind,
                user_email=user.email,
                user_id=str(user.id),
                tier=new_tier,
                amount_usd=TIER_USD_PRICE.get(new_tier),
                extra_lines=[
                    f"Was: {prior_tier or '(none)'} → Now: {new_tier}",
                    f"Stripe event: `{event_type}`",
                ],
            )
        # Rationale: tier-change revenue alert must never block the subscription webhook handler
        except Exception:  # noqa: BLE001
            logger.exception("revenue_alerts: tier-change dispatch failed")

    return {"processed": event_type, "user_id": str(user.id)}


# ── Downgrade (Pro+ → Pro) ───────────────────────────────────────────────


def downgrade_pro_plus_to_pro(user: User, db: Session) -> dict[str, Any]:
    """Switch a Pro+ subscriber to Pro with proration.

    Uses Stripe `subscription.modify` with proration_behavior="create_prorations"
    so the user receives a prorated credit for the unused portion of Pro+.
    """
    # Accept legacy slugs via shim
    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    effective_tier = _normalise_tier(user.subscription_tier) or user.subscription_tier
    if effective_tier != "pro_plus":
        raise SubscriptionError(f"not_pro_plus: current tier is {user.subscription_tier!r}")
    if not user.subscription_id:
        raise SubscriptionError("no_active_subscription")

    pro_price = TIER_PRICE_IDS.get("pro")
    if not pro_price:
        raise SubscriptionError("pro_price_not_configured")

    sub = stripe.Subscription.retrieve(user.subscription_id)
    items_data = (sub.get("items") or {}).get("data") if hasattr(sub, "get") else []
    items_data = list(items_data or [])
    if not items_data:
        raise SubscriptionError("subscription_has_no_items")
    item_id = items_data[0].get("id")
    if not item_id:
        raise SubscriptionError("subscription_item_id_missing")

    modified = stripe.Subscription.modify(
        user.subscription_id,
        items=[{"id": item_id, "price": pro_price}],
        proration_behavior="create_prorations",
        metadata={"wiserecipes_user_id": str(user.id), "tier": "pro"},
    )
    user.subscription_tier = "pro"
    db.commit()
    logger.info("Downgraded user %s pro_plus→pro (sub %s)", user.id, user.subscription_id)
    return {
        "ok": True,
        "subscription_id": user.subscription_id,
        "tier": "pro",
        "stripe_status": modified.get("status") if hasattr(modified, "get") else None,
    }


def downgrade_operator_to_cook(user: User, db: Session) -> dict[str, Any]:
    """Deprecated wrapper — delegates to downgrade_pro_plus_to_pro.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    Kept for any external caller (test code, ops scripts) that imports the
    old function name. All internal callers have been updated to use
    downgrade_pro_plus_to_pro directly.
    """
    logger.warning(
        "DEPRECATION: downgrade_operator_to_cook() is deprecated. "
        "Use downgrade_pro_plus_to_pro() instead. "
        "This wrapper will be removed after 2026-06-10 (RCP-INCIDENT-2026-05-11)."
    )
    return downgrade_pro_plus_to_pro(user, db)


def downgrade_studio_to_cook(user: User, db: Session) -> dict[str, Any]:
    """Deprecated wrapper — delegates to downgrade_pro_plus_to_pro.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    Kept for any external caller (test code, ops scripts) that imports the
    old function name. All internal callers have been updated to use
    downgrade_pro_plus_to_pro directly.
    """
    logger.warning(
        "DEPRECATION: downgrade_studio_to_cook() is deprecated. "
        "Use downgrade_pro_plus_to_pro() instead. "
        "This wrapper will be removed after 2026-06-10 (RCP-INCIDENT-2026-05-11)."
    )
    return downgrade_pro_plus_to_pro(user, db)


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
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError as e:
        raise SubscriptionError(f"Invalid signature: {e}") from e
    # Rationale: Stripe SDK may throw non-SignatureVerificationError on bad payload; wrap all
    except Exception as e:  # noqa: BLE001
        raise SubscriptionError(f"Webhook verification failed: {e}") from e


# ── Referral payout accrual (WIS-660) ────────────────────────────────────


def _accrue_referral_on_first_payment(user: User, db: Session) -> dict | None:
    """Accrue referrer's share to creator_payouts on user's first payment.

    Called from the invoice.payment_succeeded webhook. Reads the user's
    subscription from Stripe, computes the referrer's share at the rate
    locked on the Referral row, marks the referral converted, and creates
    a CreatorPayout(source='referral_first_invoice') row.

    Idempotent: if the Referral is already 'converted' (i.e. payout already
    accrued), returns None without double-paying.

    Returns a dict describing the new payout, or None if no referral exists,
    no subscription is found, or the payout was already accrued.
    """
    referral = db.query(Referral).filter(Referral.referred_user_id == user.id).first()
    if not referral:
        return None
    if referral.status == "converted":
        # Already accrued — do not double-pay on subsequent invoices.
        return None

    referrer = db.query(User).filter(User.id == referral.referrer_user_id).first()
    if not referrer:
        return None

    if not user.subscription_id:
        return None

    # Fetch subscription from Stripe to get amount.
    try:
        sub = stripe.Subscription.retrieve(user.subscription_id, expand=["items.data.plan"])
        items = (sub.get("items") or {}).get("data") or []
        if not items:
            return None
        amount = items[0].get("plan", {}).get("amount")
        if not amount:
            return None
    # Rationale: Stripe Subscription.retrieve failure must return None to skip referral accrual
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to fetch subscription %s: %s", user.subscription_id, e)
        return None

    from decimal import Decimal

    rate = referral.rate or Decimal("0.50")
    reward_cents = int(int(amount) * float(rate))

    # Mark referral converted.
    referral.status = "converted"
    referral.reward_cents = reward_cents
    referral.converted_at = datetime.now(UTC)

    # Create payout row. amount_cents and creator_share_cents both carry
    # the reward — amount_cents is the WIS-660 multi-source field, and
    # creator_share_cents is the legacy field expected by existing payout
    # tooling so it shows up in payout reports without a schema fork.
    payout = CreatorPayout(
        creator_id=referrer.id,
        creator_share_cents=reward_cents,
        amount_cents=reward_cents,
        source="referral_first_invoice",
        referral_id=referral.id,
        status="pending",
    )
    db.add(payout)
    db.commit()
    db.refresh(payout)
    logger.info(
        "Accrued referral payout: referrer=%s amount=%d cents rate=%s",
        referrer.id,
        reward_cents,
        rate,
    )
    return {"payout_id": str(payout.id), "creator_share_cents": reward_cents}


def handle_invoice_payment_succeeded(event: dict, db: Session) -> dict:
    """Handle invoice.payment_succeeded: accrue referral payout if applicable."""
    invoice = event.get("data", {}).get("object", {}) or {}
    customer_id = invoice.get("customer")
    if not customer_id:
        return {"skipped": "no-customer"}

    user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
    if not user:
        logger.warning("No user found for Stripe customer %s", customer_id)
        return {"skipped": "user-not-found"}

    payout_result = _accrue_referral_on_first_payment(user, db)
    if payout_result:
        return {
            "processed": "invoice.payment_succeeded",
            "payout": payout_result,
            "user_id": str(user.id),
        }
    return {
        "processed": "invoice.payment_succeeded",
        "referral": "none",
        "user_id": str(user.id),
    }
