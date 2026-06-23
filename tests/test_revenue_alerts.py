"""Tests for app.revenue_alerts.

Covers the unit-level behaviour: payload shape, no-op silence when env is
unset, and graceful failure when the HTTP call errors. Does NOT exercise
the actual Stripe webhook flow — that's covered by the existing
test_subscription_service tests, which still pass after this change because
the dispatch is fire-and-forget on a daemon thread.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from app import revenue_alerts


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

def test_build_embed_payload_full() -> None:
    payload = revenue_alerts._build_embed_payload(
        event_kind="new_subscription",
        user_email="alice@example.com",
        user_id="11111111-1111-1111-1111-111111111111",
        tier="cook",
        amount_usd=9.95,
        extra_lines=["Stripe checkout: cs_test_123"],
    )
    embed = payload["embeds"][0]
    assert "💰" in embed["title"]
    assert embed["color"] == revenue_alerts._COLOR_NEW_SUB
    field_names = [f["name"] for f in embed["fields"]]
    assert "Email" in field_names
    assert "Tier" in field_names
    assert "MRR impact" in field_names
    assert "User ID" in field_names
    # Pro display label, not "cook"
    tier_field = next(f for f in embed["fields"] if f["name"] == "Tier")
    assert tier_field["value"] == "Pro"
    mrr_field = next(f for f in embed["fields"] if f["name"] == "MRR impact")
    assert mrr_field["value"] == "$9.95/mo"


def test_build_embed_payload_pro_plus_aliases() -> None:
    """Both 'operator' and 'studio' DB slugs render as 'Pro+'."""
    for slug in ("operator", "studio"):
        payload = revenue_alerts._build_embed_payload(
            event_kind="subscription_upgrade",
            user_email=None,
            user_id=None,
            tier=slug,
            amount_usd=100.0,
            extra_lines=[],
        )
        tier_field = next(
            f for f in payload["embeds"][0]["fields"] if f["name"] == "Tier"
        )
        assert tier_field["value"] == "Pro+", f"slug={slug!r}"


def test_build_embed_payload_minimal() -> None:
    """All optional fields can be None — payload still valid."""
    payload = revenue_alerts._build_embed_payload(
        event_kind="subscription_canceled",
        user_email=None,
        user_id=None,
        tier=None,
        amount_usd=None,
        extra_lines=[],
    )
    embed = payload["embeds"][0]
    assert embed["title"].endswith("Subscription Canceled")
    assert embed["color"] == revenue_alerts._COLOR_CANCEL
    assert embed["fields"] == []


# ---------------------------------------------------------------------------
# No-op when neither transport configured
# ---------------------------------------------------------------------------

def test_post_revenue_event_silent_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env → no thread → no exception. Silent."""
    monkeypatch.delenv("RECIPES_REVENUE_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RECIPES_REVENUE_CHANNEL_ID", raising=False)

    with patch.object(threading, "Thread") as mock_thread:
        revenue_alerts.post_revenue_event(
            event_kind="new_subscription",
            user_email="x@y.z",
            user_id="x",
            tier="cook",
            amount_usd=9.95,
        )
        mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook URL path is preferred when both are set
# ---------------------------------------------------------------------------

def test_send_prefers_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both webhook URL and bot token are set, webhook wins."""
    fake_response = MagicMock(status_code=204, text="")
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.post = MagicMock(return_value=fake_response)

    with patch("app.revenue_alerts.httpx.Client", return_value=fake_client):
        revenue_alerts._send(
            payload={"embeds": [{"title": "test"}]},
            webhook_url="https://discord.com/api/webhooks/123/abc",
            bot_token="bot.token.value",
            channel_id="999",
        )

    fake_client.post.assert_called_once()
    args, kwargs = fake_client.post.call_args
    assert args[0] == "https://discord.com/api/webhooks/123/abc"
    assert kwargs["json"]["embeds"][0]["title"] == "test"


def test_send_falls_back_to_bot_when_no_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bot path is used only when webhook URL is empty."""
    fake_response = MagicMock(status_code=200, text="")
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.post = MagicMock(return_value=fake_response)

    with patch("app.revenue_alerts.httpx.Client", return_value=fake_client):
        revenue_alerts._send(
            payload={"content": "hi"},
            webhook_url="",
            bot_token="bot.token.value",
            channel_id="999",
        )

    fake_client.post.assert_called_once()
    args, kwargs = fake_client.post.call_args
    assert args[0] == "https://discord.com/api/v10/channels/999/messages"
    assert kwargs["headers"]["Authorization"] == "Bot bot.token.value"


# ---------------------------------------------------------------------------
# Failures don't propagate
# ---------------------------------------------------------------------------

def test_send_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network error in _send must NEVER raise — it would block Stripe webhooks."""
    with patch(
        "app.revenue_alerts.httpx.Client",
        side_effect=RuntimeError("DNS error"),
    ):
        # Should not raise
        revenue_alerts._send(
            payload={"content": "hi"},
            webhook_url="https://discord.com/api/webhooks/123/abc",
            bot_token="",
            channel_id="",
        )
