"""feat/founding — $49 one-time Founding Member SKU (capped 100 seats).

Grants permanent Pro entitlement to the first ``slot_cap`` buyers. This is a
Stripe Checkout ``mode=payment`` (ONE-TIME) purchase, NOT a subscription —
see the ``stripe-one-time-sku-on-subscription-rail`` skill for the pattern
this module follows.

SSOT: the price and the cap live ONLY in ``config/tiers.yaml``'s sibling
``founding:`` key (outside ``tiers:``, so every existing tier loader that
iterates ``data["tiers"]`` never sees it — it can never become a recurring
subscription). Read them via :func:`founding_price_usd` /
:func:`founding_slot_cap` / :func:`founding_price_id`; never hardcode 49 or
100 anywhere else in this file or in the routes that call it.

Over-sell protection is DB-authoritative (see :func:`grant_founding_membership`):
a UNIQUE constraint on ``User.founding_slot_number`` assigned ``MAX(slot)+1``
under a real write-lock, not an advisory pre-flight count. Seats remaining is
counted from that same column, never from the boolean flag alone, so a
half-written row cannot under-count and let the cap be exceeded.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import stripe
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User

logger = logging.getLogger(__name__)

_TIERS_YAML = Path(__file__).resolve().parent.parent.parent / "config" / "tiers.yaml"

# The metadata discriminator that routes a mode=payment checkout session (and
# its resulting PaymentIntent) to the founding-grant handler. Kept as a
# module constant (not a magic string re-typed at each call site) — the
# webhook handler, the checkout-session builder, and the refund path must all
# agree on this exact value.
FOUNDING_KIND = "founding"


class FoundingSoldOutError(Exception):
    """Raised when the founding cap is already reached (fail-closed)."""


@lru_cache(maxsize=1)
def _load_founding_config() -> dict[str, Any]:
    """Load the ``founding:`` sibling key from config/tiers.yaml.

    A SIBLING of ``tiers:``, not a member of it — see module docstring.
    Returns {} if the key is absent (feature not configured), so callers
    fail closed rather than crash.
    """
    import yaml

    with open(_TIERS_YAML) as f:
        data = yaml.safe_load(f) or {}
    return data.get("founding") or {}


def founding_configured() -> bool:
    """True when config/tiers.yaml declares a founding SKU at all."""
    return bool(_load_founding_config())


def founding_price_usd() -> float:
    """The one-time USD price. SOLE source: config/tiers.yaml `founding.price_usd`."""
    return float(_load_founding_config().get("price_usd", 0))


def founding_slot_cap() -> int:
    """The seat cap. SOLE source: config/tiers.yaml `founding.slot_cap`."""
    return int(_load_founding_config().get("slot_cap", 0))


def founding_grants_tier() -> str:
    """The subscription_tier granted permanently on purchase."""
    return str(_load_founding_config().get("grants_tier", "pro"))


def founding_display_name() -> str:
    return str(_load_founding_config().get("display_name", "Founding Member"))


def founding_price_id() -> str:
    """Resolve the Stripe price id via the env var named in config.

    Mirrors subscription_service._load_tier_price_ids()'s settings-attr
    resolution but for the single founding SKU (no legacy fallback — this is
    a brand-new SKU with no prior env var name).
    """
    cfg = _load_founding_config()
    env_name = cfg.get("price_id_env")
    if not env_name:
        return ""
    attr = str(env_name).removeprefix("WR_")
    return getattr(settings, attr, None) or ""


# ── Seat accounting ─────────────────────────────────────────────────────


def founding_seats_taken(db: Session) -> int:
    """Count of GENUINELY granted seats.

    Counted by the slot-number column (``IS NOT NULL``), never the boolean
    flag alone — a half-written row (flag set, slot not yet committed) can't
    then under-count and let the cap be exceeded.
    """
    return int(
        db.execute(
            select(func.count()).select_from(User).where(User.founding_slot_number.is_not(None))
        ).scalar_one()
    )


def founding_seats_remaining(db: Session) -> int:
    """Seats left, floored at 0. Fail-closed when the SKU isn't configured."""
    if not founding_configured():
        return 0
    cap = founding_slot_cap()
    taken = founding_seats_taken(db)
    return max(0, cap - taken)


# ── Checkout session ────────────────────────────────────────────────────


def create_founding_checkout_session(
    user: User,
    db: Session,
    *,
    get_or_create_customer,
    success_url: str | None = None,
    cancel_url: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session (mode=payment) for the Founding SKU.

    Fails closed BEFORE ever creating a Stripe object: a pre-flight check
    that seats remain is advisory (it does not stop a race), but it does
    stop the common case of a sold-out visitor incurring API calls at all.
    The AUTHORITATIVE guard is in :func:`grant_founding_membership`, which
    runs at webhook time.

    ``get_or_create_customer`` is injected (not imported) to avoid a circular
    import with app.subscription_service, and so the founding rail reuses
    the SAME Stripe customer a later subscription checkout would use.
    """
    if not founding_configured():
        raise FoundingSoldOutError("founding_not_configured")

    price_id = founding_price_id()
    if not price_id:
        raise FoundingSoldOutError("founding_price_not_configured")

    remaining = founding_seats_remaining(db)
    if remaining <= 0:
        raise FoundingSoldOutError("founding_sold_out")

    if user.founding_member and user.founding_slot_number is not None:
        raise FoundingSoldOutError("already_founding_member")

    customer_id = get_or_create_customer(user, db)

    base = settings.OAUTH_REDIRECT_BASE.rstrip("/") if settings.OAUTH_REDIRECT_BASE else ""
    success_url = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = cancel_url or f"{base}/pricing"

    metadata = {
        "loopskill_user_id": str(user.id),
        "kind": FOUNDING_KIND,
    }

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="payment",
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        automatic_tax={"enabled": True},
        tax_id_collection={"enabled": True},
        customer_update={"address": "auto", "name": "auto"},
        billing_address_collection="required",
        metadata=metadata,
        # Propagate the discriminator onto the PaymentIntent too — the
        # refund path (on a lost over-sell race) reads it off the
        # PaymentIntent, not the (by-then-gone) Checkout Session.
        payment_intent_data={"metadata": metadata},
        # Deterministic idempotency: same user + founding price → same
        # request. A hammering frontend or retried network call produces
        # exactly one session, never two.
        idempotency_key=f"founding_checkout_{user.id}",
    )
    logger.info("Created founding checkout session %s for user %s", session["id"], user.id)
    return {"session_id": session["id"], "url": session["url"], "tier": FOUNDING_KIND}


# ── Grant (webhook-driven) ──────────────────────────────────────────────


def grant_founding_membership(user: User, db: Session) -> dict[str, Any]:
    """Grant a founding seat to ``user``. DB-authoritative, idempotent.

    Idempotent replay: if this user already holds a seat, return
    ``{"granted": False, "replay": True}`` without touching anything —
    covers both a Stripe webhook redelivery AND a second checkout attempt by
    the same already-seated user.

    Fail-closed on the cap: takes ``MAX(founding_slot_number) + 1`` as the
    candidate seat and commits. If a concurrent grant already committed a
    slot with same the number (the same MAX+1 read under a race), commit
    raises IntegrityError on the UNIQUE constraint; the caller must treat
    that — and an explicit cap check — as sold-out and refund the charge.

    ``needs_reconcile`` shape parity: sets exactly the four fields specified
    (subscription_tier='pro', subscription_status='active', period_end=NULL)
    plus subscription_id=None — matching scripts/grant_comp_tier.py's comp
    grant byte-for-byte, so this row can never trip
    checkout_routes.billing_me's ``needs_reconcile`` re-sync (which fires
    whenever ``stripe_customer_id`` is set AND (tier is None OR status not in
    {active, trialing})). A founding grant with status='active' is never
    "unhealthy", so the reconciler leaves it alone forever.
    """
    if user.founding_member and user.founding_slot_number is not None:
        return {"granted": False, "replay": True, "slot": user.founding_slot_number}

    if not founding_configured():
        raise FoundingSoldOutError("founding_not_configured")

    cap = founding_slot_cap()
    # MAX(slot)+1, not count()+1 — count() undercounts if a row were ever
    # deleted, which would let a later grant re-issue an already-refunded
    # slot number. MAX is the seat-number source of truth per the skill's
    # over-sell-protection pattern.
    current_max = db.execute(select(func.max(User.founding_slot_number))).scalar_one() or 0
    if current_max >= cap:
        raise FoundingSoldOutError("founding_sold_out")

    next_slot = current_max + 1
    if next_slot > cap:
        raise FoundingSoldOutError("founding_sold_out")

    user.founding_member = True
    user.founding_slot_number = next_slot
    user.subscription_tier = founding_grants_tier()
    user.subscription_status = "active"
    user.subscription_current_period_end = None
    user.subscription_id = None
    try:
        db.commit()
    except IntegrityError:
        # Lost the race for `next_slot` — another concurrent grant committed
        # it first. Roll back and refuse; the caller refunds the charge.
        db.rollback()
        raise FoundingSoldOutError("founding_sold_out") from None

    db.refresh(user)
    logger.info("Granted founding seat #%s to user %s", user.founding_slot_number, user.id)
    return {"granted": True, "replay": False, "slot": user.founding_slot_number}


def handle_founding_checkout_completed(event: dict, db: Session) -> dict:
    """Handle a checkout.session.completed event for the founding SKU.

    Called from app.subscription_service.handle_checkout_completed, keyed on
    mode=payment + metadata.kind=founding (see that function's docstring for
    the routing rationale). Resolves the user the same way the subscription
    path does (metadata.loopskill_user_id, falling back to customer match),
    grants via :func:`grant_founding_membership`, and on a lost cap race
    auto-refunds the charge so a buyer is never charged for a seat they
    cannot get.
    """
    # Local import: subscription_service imports this module lazily inside
    # handle_checkout_completed, so importing it back here at call time (not
    # at module load) avoids any import-order fragility between the two.
    from app.subscription_service import _user_from_subscription_metadata

    session = event["data"]["object"]
    if session.get("payment_status") != "paid":
        return {"skipped": f"payment_status={session.get('payment_status')}"}

    user = _user_from_subscription_metadata(session, db)
    if not user:
        logger.warning("No user found for founding checkout session %s", session.get("id"))
        return {"skipped": "user-not-found"}

    payment_intent_id = session.get("payment_intent")
    try:
        result = grant_founding_membership(user, db)
    except FoundingSoldOutError:
        logger.warning(
            "Founding grant lost the cap race for user %s (session %s) — refunding",
            user.id,
            session.get("id"),
        )
        refund_lost_race(payment_intent_id)
        return {"processed": "checkout.session.completed", "user_id": str(user.id), "refunded": True}

    logger.info(
        "Founding membership %s for user %s via checkout %s (slot %s)",
        "granted" if result["granted"] else "replayed",
        user.id,
        session.get("id"),
        result.get("slot"),
    )
    return {"processed": "checkout.session.completed", "user_id": str(user.id), **result}


def refund_lost_race(payment_intent_id: str | None) -> None:
    """Best-effort refund when a buyer paid but lost the seat race.

    Never raises — a crash here would make Stripe retry the whole webhook
    delivery, potentially double-processing an already-handled event. Silent
    on a missing payment_intent_id (nothing to refund, e.g. a $0 test event).
    """
    if not payment_intent_id:
        return
    try:
        stripe.Refund.create(
            payment_intent=payment_intent_id,
            idempotency_key=f"founding_soldout_refund_{payment_intent_id}",
        )
        logger.warning(
            "Refunded payment_intent %s — founding cap reached before grant could apply",
            payment_intent_id,
        )
    # Rationale: refund is best-effort cleanup; a failure here must never crash the webhook
    except Exception:  # noqa: BLE001
        logger.exception("Failed to auto-refund lost-race payment_intent %s", payment_intent_id)
