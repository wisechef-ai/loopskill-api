"""Revenue event alerts — ping Discord on subscription state changes.

Wires into the Stripe webhook handlers so the team learns about a new paid
customer (or a cancellation) within seconds, not on the next weekly DB review.

Two transports, in priority order:

1. **Discord webhook URL** — set ``RECIPES_REVENUE_WEBHOOK_URL`` in .env. No
   bot token required, no channel discovery required, no Discord intents to
   configure. Just create a webhook in any Discord channel
   (Server Settings → Integrations → Webhooks → New Webhook → Copy URL) and
   drop it in the env file. This is the recommended path.

2. **Bot + channel** — fallback. Set ``DISCORD_BOT_TOKEN`` and
   ``RECIPES_REVENUE_CHANNEL_ID`` (numeric Discord channel id). Reuses the
   discord_bot package that the role-sync feature already depends on.

If neither is configured, every call is a silent no-op — the function logs a
debug line and returns, so dropping this code into a fresh deployment without
the env vars set will not break anything.

All HTTP calls have a 5-second timeout, run on a background thread (not on the
webhook request thread), and never raise — failure to ping Discord must NEVER
block a Stripe webhook from returning 200, otherwise Stripe retries the event
and we double-count.
"""

from __future__ import annotations

import logging
import os
import threading
from decimal import Decimal
from typing import Any

import httpx

from app.revenue_truth import tier_list_monthly_usd

logger = logging.getLogger(__name__)

# Public function — keep the signature stable so subscription_service callers
# don't have to be aware of the underlying transport.

# Reasonable hard cap so a misconfigured Discord webhook can't block a Stripe
# webhook handler indefinitely.
_HTTP_TIMEOUT_S = 5.0

# Hex colors used in the Discord embed strip on the left edge.
_COLOR_NEW_SUB = 0x22C55E  # green — new revenue
_COLOR_UPGRADE = 0xD4A017  # gold — upsell
_COLOR_CANCEL = 0xEF4444  # red  — churn
_COLOR_OTHER = 0x6B7280  # grey — informational


def post_revenue_event(
    *,
    event_kind: str,
    user_email: str | None,
    user_id: str | None,
    tier: str | None,
    real_usd: Decimal | None,
    list_usd: Decimal | None = None,
    discount_pct: Decimal | None = None,
    extra_lines: list[str] | None = None,
) -> None:
    """Fire-and-forget Discord ping for a revenue-relevant event.

    ``event_kind`` is a short label used as the embed title. Recommended values:

      - ``"new_subscription"`` — first paid signup or reactivation
      - ``"subscription_upgrade"`` — Pro → Pro+ swap
      - ``"subscription_downgrade"`` — Pro+ → Pro swap
      - ``"subscription_canceled"`` — cancellation
      - ``"payment_failed"`` — a renewal charge failed (dunning has begun)
      - ``"subscription_updated"`` — generic state change (use sparingly)

    There is deliberately **no single ``amount`` parameter.** One ambiguous
    "amount" is precisely what let the LIST price of a tier be handed in and
    rendered as realised revenue — a 100%-comped $0.00 activation posted
    "MRR impact: $9.95/mo" for all 7 live subscriptions. The caller must now
    say which number it holds, and ``real_usd`` can only come from a Stripe
    object (see :mod:`app.revenue_truth`).

    Args:
        event_kind: short label, see above.
        user_email: paying user's email (None if not yet known).
        user_id: internal UUID, surfaced for cross-reference.
        tier: db slug — ``pro``, ``pro_plus``, ``free``, or None. Legacy slugs ``cook``, ``operator``, ``studio`` also accepted via shim until 2026-06-10.
        real_usd: REAL monthly cash Stripe bills, net of discounts, from
            :func:`app.revenue_truth.real_monthly_usd`. ``Decimal("0.00")``
            means "Stripe bills nothing" (comped — a fact). ``None`` means
            "we could not determine the amount" (unknown — not a fact); it
            renders as *unknown*, never as $0.00 and never as list price.
        list_usd: gross monthly price before discount, for contrast. Shown
            alongside ``real_usd`` whenever the two differ. NEVER revenue.
        discount_pct: exact share of list price discounted away, from
            :func:`app.revenue_truth.discount_pct`. Passed separately because it
            must be computed from exact cents — recomputing it from the two
            rounded USD figures renders a 50%-off coupon as "49.95% off".
        extra_lines: optional list of additional bullet strings.

    Never raises. If both transports are unconfigured, returns silently.
    """
    webhook_url = os.environ.get(
        "RECIPES_REVENUE_WEBHOOK_URL", ""
    ).strip()  # TODO(rename): env var still uses legacy name for prod compatibility
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get(
        "RECIPES_REVENUE_CHANNEL_ID", ""
    ).strip()  # TODO(rename): env var still uses legacy name for prod compatibility

    has_webhook = bool(webhook_url)
    has_bot = bool(bot_token and channel_id)
    if not has_webhook and not has_bot:
        logger.debug(
            "revenue alert skipped — neither RECIPES_REVENUE_WEBHOOK_URL "
            "nor DISCORD_BOT_TOKEN+RECIPES_REVENUE_CHANNEL_ID set"
        )
        return

    payload = _build_embed_payload(
        event_kind=event_kind,
        user_email=user_email,
        user_id=user_id,
        tier=tier,
        real_usd=real_usd,
        list_usd=list_usd,
        discount_pct=discount_pct,
        extra_lines=extra_lines or [],
    )

    # Run on a background thread so the webhook response thread is never
    # blocked on Discord network I/O. Daemon=True so it doesn't hold up
    # process shutdown.
    thread = threading.Thread(
        target=_send,
        args=(payload, webhook_url, bot_token, channel_id),
        daemon=True,
        name="recipes-revenue-alert",
    )
    thread.start()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_TIER_DISPLAY = {
    "free": "Free",
    "pro": "Pro",
    "pro_plus": "Pro+",
    # Legacy aliases — RCP-INCIDENT-2026-05-11 shim, remove after 2026-06-10
    "cook": "Pro",  # legacy alias → pro
    "operator": "Pro+",  # legacy alias → pro_plus
    "studio": "Pro+",  # legacy alias → pro_plus
}


def _color_for(event_kind: str) -> int:
    if event_kind == "new_subscription":
        return _COLOR_NEW_SUB
    if event_kind == "subscription_upgrade":
        return _COLOR_UPGRADE
    if event_kind == "subscription_canceled":
        return _COLOR_CANCEL
    return _COLOR_OTHER


def _emoji_for(event_kind: str) -> str:
    if event_kind == "new_subscription":
        return "💰"
    if event_kind == "subscription_upgrade":
        return "⬆️"
    if event_kind == "subscription_downgrade":
        return "⬇️"
    if event_kind == "subscription_canceled":
        return "👋"
    return "ℹ️"


def _usd(amount: Decimal) -> str:
    """Render a Decimal USD figure as ``$9.95/mo``."""
    return f"${amount:,.2f}/mo"


def _pct(pct: Decimal) -> str:
    """Render a discount percentage, e.g. ``50`` or ``33.33``."""
    pct = pct.quantize(Decimal("0.01")).normalize()
    # normalize() turns Decimal("50.00") into Decimal("5E+1"); format with 'f'
    # so it reads as "50" and not as scientific notation in a Discord message.
    return f"{pct:f}"


def _render_mrr_impact(
    *,
    real_usd: Decimal | None,
    list_usd: Decimal | None,
    discount_pct: Decimal | None,
    tier: str | None,
) -> str:
    """Render the money line for the Discord embed. The anti-phantom-MRR core.

    Three distinct states, rendered three distinct ways — conflating any two of
    them is how phantom revenue gets reported:

      * **known and full price** → ``$9.95/mo``
      * **known and discounted** → ``real $4.98/mo · list $9.95/mo · 50% off``,
        or ``real $0.00/mo · list $9.95/mo · 100% comped`` when nothing is
        billed. Both figures are always shown when they differ, so nobody has
        to guess which one they are reading.
      * **unknown** (``real_usd is None``) → the word *unknown*. The tier's list
        price may be quoted for context, but only spelled out as a ceiling that
        is NOT revenue.
    """
    if real_usd is None:
        ceiling = tier_list_monthly_usd(tier)
        if ceiling > 0:
            return f"unknown — Stripe reported no amount (list ceiling {_usd(ceiling)}, NOT revenue)"
        return "unknown — Stripe reported no amount"

    if list_usd is None or list_usd <= real_usd:
        # No contrasting list price to show. A $0.00 bill is still called comped
        # explicitly, so a $0 internal price never reads as ordinary revenue.
        if real_usd <= 0:
            return f"{_usd(real_usd)} · comped"
        return _usd(real_usd)

    if real_usd <= 0:
        label = "100% comped"
    elif discount_pct is not None:
        label = f"{_pct(discount_pct)}% off"
    else:
        # No exact percentage supplied — say the two differ without asserting a
        # figure derived from rounded dollars (that renders 50% off as 49.95%).
        label = "discounted"
    return f"real {_usd(real_usd)} · list {_usd(list_usd)} · {label}"


def _build_embed_payload(
    *,
    event_kind: str,
    user_email: str | None,
    user_id: str | None,
    tier: str | None,
    real_usd: Decimal | None,
    list_usd: Decimal | None,
    discount_pct: Decimal | None,
    extra_lines: list[str],
) -> dict[str, Any]:
    """Construct the Discord-compatible JSON body."""
    tier_display = _TIER_DISPLAY.get((tier or "").lower(), tier or "—")

    fields: list[dict[str, Any]] = []
    if user_email:
        fields.append({"name": "Email", "value": user_email, "inline": True})
    if tier:
        fields.append({"name": "Tier", "value": tier_display, "inline": True})
    if real_usd is not None or tier:
        # Always render the money line when there is a subscription in play,
        # including for a $0.00 comped activation: suppressing the alert would
        # hide that the checkout pipe is alive, which the team explicitly wants
        # to see. Honesty here is about the FIGURE, not about staying quiet.
        fields.append(
            {
                "name": "MRR impact",
                "value": _render_mrr_impact(
                    real_usd=real_usd,
                    list_usd=list_usd,
                    discount_pct=discount_pct,
                    tier=tier,
                ),
                "inline": True,
            }
        )
    if user_id:
        fields.append(
            {
                "name": "User ID",
                "value": f"`{user_id}`",
                "inline": False,
            }
        )
    for line in extra_lines:
        if line:
            fields.append({"name": "\u200b", "value": line, "inline": False})

    title = f"{_emoji_for(event_kind)} {event_kind.replace('_', ' ').title()}"
    embed = {
        "title": title,
        "color": _color_for(event_kind),
        "fields": fields,
        "footer": {"text": "app.loopskill.io"},
    }
    return {"embeds": [embed]}


def _send(
    payload: dict[str, Any],
    webhook_url: str,
    bot_token: str,
    channel_id: str,
) -> None:
    """Deliver to Discord. Webhook URL preferred; bot fallback only if absent."""
    try:
        if webhook_url:
            with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
                resp = client.post(webhook_url, json=payload)
                if resp.status_code >= 400:
                    logger.warning(
                        "revenue alert webhook returned %s: %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                else:
                    logger.info("revenue alert delivered via webhook (%s)", resp.status_code)
            return

        if bot_token and channel_id:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code >= 400:
                    logger.warning(
                        "revenue alert bot post returned %s: %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                else:
                    logger.info("revenue alert delivered via bot (%s)", resp.status_code)
    # Rationale: revenue alert delivery must never crash the webhook handler
    except Exception:  # noqa: BLE001 — we never let this crash the webhook handler
        logger.exception("revenue alert delivery failed")
