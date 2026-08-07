"""Revenue truth — the ONE module that answers "did money actually move?".

Background (mesh0408e2e W2). LoopSkill reported 7 active subscriptions and
posted "MRR impact: $9.95/mo" to Discord for every one of them. All 7 are
100%-comped: every Stripe invoice ``amount_paid`` is 0.00 and lifetime revenue
is $0.00. The alert was reading the *list price of the tier* out of a lookup
table and rendering it as realised revenue.

The DB cannot answer the revenue question: it stores only ``subscription_tier``
and ``subscription_status``, so a 100%-off promo subscription is byte-identical
to a full-price one locally. Only Stripe knows what is billed. Every helper here
therefore takes a **Stripe subscription object** and reads what Stripe actually
charges — never a tier table.

Vocabulary (shared with ``GET /api/admin/pulse``; do not invent a second one):

``real``
    What Stripe actually bills per month, net of coupons. The only number that
    may ever be called revenue.
``list``
    Gross line-item total before discount. A CEILING, never revenue.
``comped``
    A subscription whose real monthly cash is $0.00 — a 100%-off coupon or a
    $0 internal price. It is a real subscription; it is not revenue.

Money is ``Decimal`` end to end. Floats are banned here: 0.1 + 0.2 != 0.3 is not
an acceptable property for a revenue figure.

Any place that reports a revenue number and does not go through this module is a
future lie.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from app.tier_labels import _canonical

# ── Subscription liveness ────────────────────────────────────────────────────
#
# The statuses that mean "this subscription is live and entitled". Everything
# else — past_due, unpaid, incomplete, incomplete_expired, canceled, paused —
# means the customer is NOT currently entitled, even though the row still
# carries a paid ``subscription_tier``. This frozenset was previously
# copy-pasted into five modules (three of which then forgot to consult it);
# it lives here now so a failed renewal cannot silently keep serving Pro.
HEALTHY_SUB_STATUSES: frozenset[str] = frozenset({"active", "trialing"})

_ZERO = Decimal(0)
_USD_QUANTUM = Decimal("0.01")
_CENTS_PER_USD = Decimal(100)

# Path to the SSOT tier config (config/tiers.yaml, one level above app/).
_TIERS_YAML = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"


# ── Decimal coercion ────────────────────────────────────────────────────────


def _dec(value: Any) -> Decimal | None:
    """Coerce a Stripe/YAML scalar to Decimal, or None if it isn't a number.

    Goes through ``str()`` for floats so a YAML ``9.95`` becomes
    ``Decimal("9.95")`` and not ``Decimal("9.9499999999999995...")``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` as a mapping, or an empty dict.

    Stripe SDK objects are dict-like but not ``dict``; ``Mapping`` covers both
    and a non-mapping (or None) degrades to empty rather than raising inside a
    webhook handler.
    """
    return value if isinstance(value, Mapping) else {}


# ── Interval normalisation ──────────────────────────────────────────────────
#
# Stripe prices recur on day/week/month/year. MRR is a monthly figure, so every
# interval is converted with an exact Decimal factor. Yearly uses /12 and weekly
# uses 52/12 — the standard SaaS convention, not calendar-exact, and stated here
# so nobody has to reverse-engineer it from the arithmetic.
_MONTHS_PER_INTERVAL: dict[str, Decimal] = {
    "month": Decimal(1),
    "year": Decimal(12),
    "week": Decimal(12) / Decimal(52),
    "day": Decimal(12) / Decimal(365),
}


def _monthly_factor(recurring: Mapping[str, Any]) -> Decimal | None:
    """Months-per-charge divisor for a Stripe price ``recurring`` block.

    Returns None for an interval we do not recognise — the caller then skips
    that line item, so an unknown interval under-counts rather than inventing
    revenue.
    """
    interval = recurring.get("interval") or "month"
    months = _MONTHS_PER_INTERVAL.get(str(interval))
    if months is None:
        return None
    count = _dec(recurring.get("interval_count")) or Decimal(1)
    if count <= 0:
        count = Decimal(1)
    return months * count


# ── Gross ("list") monthly cash ─────────────────────────────────────────────


def list_monthly_cents(subscription: Mapping[str, Any] | None) -> Decimal:
    """Gross monthly cents a subscription's line items list at, before discount.

    This is the LIST CEILING. It is not revenue and must never be rendered as
    revenue. Exact (unrounded) Decimal so callers can sum many subscriptions
    before rounding once.

    A line item with no ``unit_amount`` (metered / tiered / unknown price)
    contributes 0: under-count rather than re-introduce phantom revenue.
    """
    sub = _as_mapping(subscription)
    gross = _ZERO
    items = _as_mapping(sub.get("items")).get("data") or []
    for raw_item in items:
        item = _as_mapping(raw_item)
        price = _as_mapping(item.get("price"))
        unit = _dec(price.get("unit_amount"))
        if unit is None:
            continue  # metered/unknown price → no fixed cash
        factor = _monthly_factor(_as_mapping(price.get("recurring")))
        if factor is None:
            continue  # unrecognised interval → skip, never guess
        qty = _dec(item.get("quantity"))
        if qty is None or qty <= 0:
            qty = Decimal(1)
        gross += (unit * qty) / factor
    return gross


def list_monthly_usd(subscription: Mapping[str, Any] | None) -> Decimal:
    """Gross monthly USD the subscription lists at, before discount.

    The LIST CEILING, quantized to cents. NOT revenue.
    """
    return _to_usd(list_monthly_cents(subscription))


# ── Coupons ─────────────────────────────────────────────────────────────────


def _coupons(subscription: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every coupon attached to the subscription.

    Stripe has carried a singular ``discount`` object for years and also
    exposes a ``discounts`` array. Read BOTH and de-duplicate by discount id:
    a handler that only understood ``discount`` would miss an array-only
    100%-off coupon and report the list price as revenue — exactly the bug
    this module exists to make impossible.
    """
    raw: list[Any] = []
    single = subscription.get("discount")
    if single:
        raw.append(single)
    many = subscription.get("discounts")
    if isinstance(many, list):
        raw.extend(many)

    coupons: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        discount = _as_mapping(entry)
        if not discount:
            continue
        discount_id = discount.get("id")
        if isinstance(discount_id, str) and discount_id:
            if discount_id in seen:
                continue
            seen.add(discount_id)
        coupon = _as_mapping(discount.get("coupon"))
        if coupon:
            coupons.append(coupon)
    return coupons


def _apply_coupons(gross: Decimal, coupons: list[Mapping[str, Any]]) -> Decimal:
    """Apply coupons to a gross monthly figure, clamped at zero.

    ``percent_off`` scales; ``amount_off`` subtracts (minor units). Multiple
    coupons compound in the order Stripe returned them. A discount can never
    make Stripe pay the customer, so the result floors at 0.
    """
    net = gross
    for coupon in coupons:
        pct = _dec(coupon.get("percent_off"))
        amt = _dec(coupon.get("amount_off"))
        if pct is not None:
            net *= max(_ZERO, Decimal(1) - (pct / Decimal(100)))
        elif amt is not None:
            net -= amt
    return max(_ZERO, net)


# ── Real ("cash") monthly revenue ───────────────────────────────────────────


def real_monthly_cents(subscription: Mapping[str, Any] | None) -> Decimal:
    """REAL monthly cents Stripe bills this subscription, net of coupons.

    Exact (unrounded) Decimal so a caller summing an MRR across subscriptions
    rounds once at the end instead of accumulating per-row rounding error.

    This is the only figure in the codebase that may be called revenue.
    """
    sub = _as_mapping(subscription)
    return _apply_coupons(list_monthly_cents(sub), _coupons(sub))


def real_monthly_usd(subscription: Mapping[str, Any] | None) -> Decimal:
    """REAL monthly USD Stripe bills this subscription, quantized to cents."""
    return _to_usd(real_monthly_cents(subscription))


def _to_usd(cents: Decimal) -> Decimal:
    """Convert exact Decimal cents to USD, rounded half-up to the cent."""
    return (cents / _CENTS_PER_USD).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


def cents_to_usd(cents: Decimal | int) -> Decimal:
    """Public wrapper over :func:`_to_usd` for callers summing exact cents."""
    return _to_usd(_dec(cents) or _ZERO)


# ── The predicate ───────────────────────────────────────────────────────────


def is_comped(subscription: Mapping[str, Any] | None) -> bool:
    """True when this subscription moves NO money.

    Covers both shapes we actually have in production: a 100%-off promotion
    code, and an internal $0 price. Neither is revenue, and the distinction
    between them does not matter to any revenue surface — what matters is that
    a comped subscription is a real, live subscription that bills $0.00.

    A subscription with no resolvable line items is comped by this definition
    (it bills nothing). Callers that need "we could not determine the amount"
    must check that separately — see :func:`has_resolvable_amount`.
    """
    return real_monthly_cents(subscription) <= _ZERO


def has_resolvable_amount(subscription: Mapping[str, Any] | None) -> bool:
    """True when the subscription carries at least one priced line item.

    Distinguishes "Stripe says this bills $0.00" (comped — a fact) from "we
    have no subscription object / no priced item" (unknown — not a fact). A
    revenue surface must render those differently; conflating them is how a
    missing object becomes a confident $0.
    """
    sub = _as_mapping(subscription)
    items = _as_mapping(sub.get("items")).get("data") or []
    for raw_item in items:
        price = _as_mapping(_as_mapping(raw_item).get("price"))
        if _dec(price.get("unit_amount")) is not None:
            return True
    return False


def discount_pct(subscription: Mapping[str, Any] | None) -> Decimal | None:
    """How much of the list price is discounted away, as a percentage.

    ``Decimal("100")`` for a fully comped subscription, ``Decimal("50")`` for a
    half-off coupon, ``Decimal(0)`` when the customer pays list. Returns None
    when the list price is $0 (nothing to take a percentage of) — a $0 internal
    price is comped but not "100% off".
    """
    sub = _as_mapping(subscription)
    gross = list_monthly_cents(sub)
    if gross <= _ZERO:
        return None
    net = _apply_coupons(gross, _coupons(sub))
    pct = (Decimal(1) - (net / gross)) * Decimal(100)
    return pct.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP).normalize()


# ── The one bundle every revenue surface should ask for ─────────────────────


class RevenueFigures(NamedTuple):
    """Every number a revenue surface needs about one subscription, consistent.

    Computed together from a single Stripe object so the three can never
    disagree. In particular ``discount_pct`` is derived from the EXACT cent
    figures, not from the rounded USD ones — dividing two already-rounded
    values renders a 50%-off coupon as "49.95% off", which reads like a bug to
    whoever is looking at the alert.

    ``real_usd`` is None when Stripe gave us nothing priced to read. That is
    *unknown*, and a caller must render it differently from ``Decimal("0.00")``
    (which is the hard fact "Stripe bills nothing").
    """

    real_usd: Decimal | None
    list_usd: Decimal | None
    discount_pct: Decimal | None


def figures_for(subscription: Mapping[str, Any] | None) -> RevenueFigures:
    """Resolve :class:`RevenueFigures` for a Stripe subscription object."""
    if subscription is None or not has_resolvable_amount(subscription):
        return RevenueFigures(None, None, None)
    return RevenueFigures(
        real_usd=real_monthly_usd(subscription),
        list_usd=list_monthly_usd(subscription),
        discount_pct=discount_pct(subscription),
    )


# ── Tier list prices (SSOT: config/tiers.yaml) ──────────────────────────────


@lru_cache(maxsize=1)
def _tier_list_prices() -> dict[str, Decimal]:
    """{canonical_slug: monthly list price} from config/tiers.yaml.

    config/tiers.yaml is the SSOT for price_usd (lock #24). Reading it here
    replaces the hand-maintained copy that had drifted to $20 for a $9.95 tier
    — a duplicated price constant is a lie waiting for someone to trust it.
    """
    with open(_TIERS_YAML) as fh:
        tiers = yaml.safe_load(fh)["tiers"] or {}
    prices: dict[str, Decimal] = {}
    for slug, meta in tiers.items():
        price = _dec((meta or {}).get("price_usd"))
        if price is not None:
            prices[slug] = price
    return prices


def tier_list_monthly_usd(tier: str | None) -> Decimal:
    """The LIST price of a tier from config/tiers.yaml. NOT revenue.

    Use only for a clearly-labelled ceiling, or to compare two tiers' rank.
    Never as the amount a specific customer paid — that is the phantom-MRR bug.
    Unknown/None tier → $0.00. Legacy slugs resolve via
    :func:`app.tier_labels._canonical`.
    """
    if not tier:
        return _ZERO.quantize(_USD_QUANTUM)
    price = _tier_list_prices().get(_canonical(tier.lower()))
    return (price if price is not None else _ZERO).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


# ── Entitlement ─────────────────────────────────────────────────────────────


def entitled_tier(user: Any) -> str | None:
    """The tier a user is entitled to RIGHT NOW, or None.

    ``User.subscription_tier`` alone is not an entitlement: it keeps its paid
    value through ``past_due``, ``unpaid``, ``incomplete`` and ``paused``. Five
    gates read the raw column and so kept serving Pro to a subscription whose
    card had failed — a failed renewal silently became free service. This is
    the status-gated answer every gate must ask for instead.

    Returns None for an anonymous/absent user so callers can treat "no user"
    and "no live subscription" identically (both are Free).
    """
    if user is None:
        return None
    if getattr(user, "subscription_status", None) not in HEALTHY_SUB_STATUSES:
        return None
    return getattr(user, "subscription_tier", None)


def entitled_tier_or_free(user: Any) -> str:
    """:func:`entitled_tier` with the explicit ``"free"`` fallback.

    For the many call sites that want a non-None slug to hand to
    ``tier_labels.bundle_limit()`` / quota helpers.
    """
    return entitled_tier(user) or "free"
