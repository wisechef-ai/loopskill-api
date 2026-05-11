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
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Public function — keep the signature stable so subscription_service callers
# don't have to be aware of the underlying transport.

# Reasonable hard cap so a misconfigured Discord webhook can't block a Stripe
# webhook handler indefinitely.
_HTTP_TIMEOUT_S = 5.0

# Hex colors used in the Discord embed strip on the left edge.
_COLOR_NEW_SUB = 0x22C55E       # green — new revenue
_COLOR_UPGRADE = 0xD4A017       # gold — upsell
_COLOR_CANCEL = 0xEF4444        # red  — churn
_COLOR_OTHER = 0x6B7280         # grey — informational


def post_revenue_event(
    *,
    event_kind: str,
    user_email: str | None,
    user_id: str | None,
    tier: str | None,
    amount_usd: float | None,
    extra_lines: list[str] | None = None,
) -> None:
    """Fire-and-forget Discord ping for a revenue-relevant event.

    ``event_kind`` is a short label used as the embed title. Recommended values:

      - ``"new_subscription"`` — first paid signup or reactivation
      - ``"subscription_upgrade"`` — Pro → Pro+ swap
      - ``"subscription_downgrade"`` — Pro+ → Pro swap
      - ``"subscription_canceled"`` — cancellation
      - ``"subscription_updated"`` — generic state change (use sparingly)

    Args:
        event_kind: short label, see above.
        user_email: paying user's email (None if not yet known).
        user_id: internal UUID, surfaced for cross-reference.
        tier: db slug — ``pro``, ``pro_plus``, ``free``, or None. Legacy slugs ``cook``, ``operator``, ``studio`` also accepted via shim until 2026-06-10.
        amount_usd: monthly subscription price in USD (None if unknown).
        extra_lines: optional list of additional bullet strings.

    Never raises. If both transports are unconfigured, returns silently.
    """
    webhook_url = os.environ.get("RECIPES_REVENUE_WEBHOOK_URL", "").strip()
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("RECIPES_REVENUE_CHANNEL_ID", "").strip()

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
        amount_usd=amount_usd,
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
    "cook": "Pro",
    "operator": "Pro+",
    "studio": "Pro+",
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


def _build_embed_payload(
    *,
    event_kind: str,
    user_email: str | None,
    user_id: str | None,
    tier: str | None,
    amount_usd: float | None,
    extra_lines: list[str],
) -> dict[str, Any]:
    """Construct the Discord-compatible JSON body."""
    tier_display = _TIER_DISPLAY.get((tier or "").lower(), tier or "—")

    fields: list[dict[str, Any]] = []
    if user_email:
        fields.append({"name": "Email", "value": user_email, "inline": True})
    if tier:
        fields.append({"name": "Tier", "value": tier_display, "inline": True})
    if amount_usd is not None:
        fields.append({
            "name": "MRR impact",
            "value": f"${amount_usd:.2f}/mo",
            "inline": True,
        })
    if user_id:
        fields.append({
            "name": "User ID",
            "value": f"`{user_id}`",
            "inline": False,
        })
    for line in extra_lines:
        if line:
            fields.append({"name": "\u200b", "value": line, "inline": False})

    title = f"{_emoji_for(event_kind)} {event_kind.replace('_', ' ').title()}"
    embed = {
        "title": title,
        "color": _color_for(event_kind),
        "fields": fields,
        "footer": {"text": "recipes.wisechef.ai"},
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
    except Exception:  # noqa: BLE001 — we never let this crash the webhook handler
        logger.exception("revenue alert delivery failed")
