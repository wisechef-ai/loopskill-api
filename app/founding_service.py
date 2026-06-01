"""Founding Integrator SKU service (loopclose_3005 Phase D).

The Founding Integrator is a ONE-TIME payment (Stripe Checkout ``mode=payment``)
that grants the buyer lifetime ``pro_plus`` access, capped at a fixed number of
seats. It is deliberately NOT a subscription tier — it lives outside the
``tiers:`` map in ``config/tiers.yaml`` (a sibling ``founding:`` block) so it can
never leak into ``TIER_PRICE_IDS`` and be checked out as a recurring plan.

This module owns:
  - SSOT reads of the founding config (price, slot cap, grant tier, price id)
  - the server-side seat counter (how many founding seats are sold)
  - ``create_founding_checkout_session`` — a one-time Checkout Session, with a
    pre-flight cap check so a sold-out SKU never reaches Stripe
  - ``grant_founding_membership`` — webhook-side grant that assigns the next
    seat number atomically and is replay-safe

Over-sell protection is two-layered:
  1. **Pre-check** at checkout: refuse to start a session when seats are gone.
     This is advisory (a race between two buyers could let two sessions start).
  2. **Authoritative** at grant: ``founding_slot_number`` carries a UNIQUE
     constraint and is assigned as ``MAX(slot)+1``. If two grants race for the
     last seat, the DB rejects the second with an IntegrityError, which we
     translate into a refund-eligible ``FoundingSoldOutError`` — the buyer is
     never silently charged for a seat they can't get. The premortem F8.1 term
     (deploy-within-N-days-or-convert/refund) is an explicit up-front promise,
     so a refunded over-sell is a clean, expected branch — not a clawback.

The $1000 price and the 25-seat cap exist ONLY in ``config/tiers.yaml``. Nothing
in this module hardcodes either number.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import stripe
import yaml
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User

logger = logging.getLogger(__name__)

# config/tiers.yaml lives two levels up from app/ (same anchor tier_labels uses)
_TIERS_YAML = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"


class FoundingError(Exception):
    """Base error for founding-SKU operations."""


class FoundingNotConfiguredError(FoundingError):
    """Raised when the founding SKU isn't wired up (no config block or price id)."""


class FoundingSoldOutError(FoundingError):
    """Raised when all founding seats are taken."""


# ── SSOT config reads ────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _founding_cfg() -> dict[str, Any]:
    """Load and cache the ``founding:`` block from config/tiers.yaml.

    Returns an empty dict when the block is absent (founding sales not enabled
    on this deployment). Callers must handle the empty case.
    """
    with open(_TIERS_YAML) as f:
        data = yaml.safe_load(f) or {}
    return data.get("founding") or {}


def founding_enabled() -> bool:
    """True when a founding config block exists AND a Stripe price is wired."""
    return bool(_founding_cfg()) and bool(founding_price_id())


def founding_slot_cap() -> int:
    """Total number of founding seats. SOLE numeric source: tiers.yaml slot_cap.

    Raises FoundingNotConfiguredError if the founding block is missing entirely
    (so a misconfigured deployment fails loud, not with a silent cap of 0).
    """
    cfg = _founding_cfg()
    if not cfg:
        raise FoundingNotConfiguredError("No `founding` block in config/tiers.yaml")
    return int(cfg["slot_cap"])


def founding_price_usd() -> float:
    """Display/alert price in USD. Authoritative amount lives on the Stripe price."""
    cfg = _founding_cfg()
    if not cfg:
        raise FoundingNotConfiguredError("No `founding` block in config/tiers.yaml")
    return float(cfg["price_usd"])


def founding_grant_tier() -> str:
    """The subscription_tier a founding purchase grants (e.g. 'pro_plus')."""
    cfg = _founding_cfg()
    if not cfg:
        raise FoundingNotConfiguredError("No `founding` block in config/tiers.yaml")
    return str(cfg["grants_tier"])


def founding_display_name() -> str:
    """Human label, e.g. 'Founding Integrator'."""
    cfg = _founding_cfg()
    return str(cfg.get("display_name", "Founding Integrator")) if cfg else "Founding Integrator"


def founding_price_id() -> str:
    """Resolve the Stripe one-time price ID from the env var named in the SSOT.

    Returns "" when either the config block or the env value is missing.
    """
    cfg = _founding_cfg()
    if not cfg:
        return ""
    env_name = cfg.get("price_id_env")
    if not env_name:
        return ""
    attr = env_name.removeprefix("WR_")  # pydantic env_prefix=WR_
    return (getattr(settings, attr, None) or "").strip()


# ── Seat counter ─────────────────────────────────────────────────────────


def founding_seats_taken(db: Session) -> int:
    """Number of founding seats currently allocated.

    Counts users with a non-null founding_slot_number (the authoritative
    seat-assignment column), NOT founding_member, so a half-written row can
    never under-count and let the cap be exceeded.
    """
    return int(db.query(func.count(User.id)).filter(User.founding_slot_number.isnot(None)).scalar() or 0)


def founding_seats_remaining(db: Session) -> int:
    """Seats still available (never negative)."""
    return max(0, founding_slot_cap() - founding_seats_taken(db))


def founding_is_sold_out(db: Session) -> bool:
    """True when no founding seats remain."""
    return founding_seats_remaining(db) <= 0


# ── Checkout (one-time payment) ──────────────────────────────────────────


def create_founding_checkout_session(
    user: User,
    db: Session,
    success_url: str | None = None,
    cancel_url: str | None = None,
    utm_ref: str | None = None,
) -> dict[str, Any]:
    """Create a one-time Stripe Checkout Session for a founding seat.

    Pre-flight checks (fail before touching Stripe):
      - founding must be configured (block + price id) → FoundingNotConfiguredError
      - the user must not already be a founding member (idempotent UX)
      - seats must remain → FoundingSoldOutError

    Returns ``{session_id, url, kind: 'founding'}``. The ``mode=payment`` and
    metadata.kind=founding are what the webhook routes on.
    """
    price_id = founding_price_id()
    if not price_id:
        raise FoundingNotConfiguredError(
            "Founding SKU not configured (WR_STRIPE_PRICE_FOUNDING unset or no founding block)"
        )

    if user.founding_member:
        raise FoundingError("already_founding_member")

    # Advisory pre-check — the authoritative gate is the unique slot at grant.
    if founding_is_sold_out(db):
        raise FoundingSoldOutError(f"All {founding_slot_cap()} founding seats are taken")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = "2026-01-28.clover"

    # Lazily ensure a Stripe customer (reuse the subscription path's helper so
    # one customer object is shared across founding + any later subscription).
    from app.subscription_service import get_or_create_customer

    customer_id = get_or_create_customer(user, db)

    base = settings.OAUTH_REDIRECT_BASE.rstrip("/") if settings.OAUTH_REDIRECT_BASE else ""
    success_url = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = cancel_url or f"{base}/pricing"

    metadata = {
        "wiserecipes_user_id": str(user.id),
        "kind": "founding",
        **({"utm_ref": utm_ref} if utm_ref else {}),
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
        payment_intent_data={"metadata": metadata},
        # One session per user for the founding SKU within Stripe's 24h window.
        idempotency_key=f"founding_checkout_{user.id}",
    )
    logger.info("Created founding checkout session %s for user %s", session["id"], user.id)
    return {"session_id": session["id"], "url": session["url"], "kind": "founding"}


# ── Grant (webhook-side) ─────────────────────────────────────────────────


def grant_founding_membership(user: User, db: Session) -> dict[str, Any]:
    """Grant lifetime founding membership: assign the next seat + set pro_plus.

    Atomic + replay-safe:
      - If the user is ALREADY a founding member (replay of the same paid
        event, or a second webhook for the same session), this is a no-op that
        returns the existing seat — never double-assigns or errors.
      - Otherwise assigns ``MAX(founding_slot_number)+1``. The UNIQUE constraint
        on that column means a concurrent grant racing for the same number loses
        with IntegrityError, which we surface as FoundingSoldOutError so the
        caller can refund (the seat genuinely wasn't available).

    Sets: founding_member=True, founding_slot_number=N, subscription_tier=<grant
    tier>, subscription_status='active'. Does NOT set subscription_id (there is
    no recurring subscription behind a one-time founding payment).
    """
    # Idempotent replay: already granted → return current seat unchanged.
    if user.founding_member and user.founding_slot_number is not None:
        logger.info(
            "Founding grant replay for user %s — already seat #%s, no-op",
            user.id,
            user.founding_slot_number,
        )
        return {
            "granted": False,
            "replay": True,
            "slot_number": user.founding_slot_number,
            "user_id": str(user.id),
        }

    cap = founding_slot_cap()
    grant_tier = founding_grant_tier()

    # Authoritative cap gate: compute next seat as MAX+1.
    current_max = int(db.query(func.max(User.founding_slot_number)).scalar() or 0)
    next_slot = current_max + 1
    if next_slot > cap:
        raise FoundingSoldOutError(
            f"Founding seat grant refused — {cap} seats already allocated (refund-eligible)"
        )

    user.founding_member = True
    user.founding_slot_number = next_slot
    user.subscription_tier = grant_tier
    user.subscription_status = "active"

    try:
        db.commit()
    except IntegrityError as exc:
        # Lost the race for `next_slot` (unique violation). Roll back and signal
        # sold-out so the webhook caller can refund the one-time charge.
        db.rollback()
        logger.warning(
            "Founding seat #%s collision for user %s — concurrent grant won; refund-eligible",
            next_slot,
            user.id,
        )
        raise FoundingSoldOutError(
            f"Founding seat #{next_slot} taken by a concurrent grant (refund-eligible)"
        ) from exc

    db.refresh(user)
    logger.info(
        "Granted founding membership to user %s: seat #%s, tier=%s",
        user.id,
        user.founding_slot_number,
        user.subscription_tier,
    )
    return {
        "granted": True,
        "replay": False,
        "slot_number": user.founding_slot_number,
        "tier": user.subscription_tier,
        "user_id": str(user.id),
    }
