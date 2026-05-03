"""Phase D — Discord bot tests.

The Discord server doesn't exist yet (Adam will create it later). The bot must
no-op gracefully without DISCORD_BOT_TOKEN. When the token is set, role
assignment fires from Stripe webhook events.
"""
from __future__ import annotations

import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.discord_bot import bot as bot_module
from app.discord_bot.role_sync import role_for_user, sync_role_for_user


def test_lifespan_noop_without_token(monkeypatch, caplog):
    """If DISCORD_BOT_TOKEN is absent, start_bot() returns None and logs."""
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    caplog.set_level(logging.INFO, logger="wiserecipes.discord")
    task = asyncio.run(bot_module.start_bot())
    assert task is None
    assert any(
        "disabled" in rec.message.lower() for rec in caplog.records
    ), "lifespan must log that it skipped startup"


def test_role_for_user_studio():
    user = MagicMock(subscription_tier="studio", subscription_status="active")
    assert role_for_user(user) == "All-in"


def test_role_for_user_cook():
    user = MagicMock(subscription_tier="cook", subscription_status="active")
    assert role_for_user(user) == "Pro"


def test_role_for_user_canceled_falls_back_to_free():
    user = MagicMock(
        subscription_tier="studio", subscription_status="canceled"
    )
    assert role_for_user(user) == "Free"


def test_role_for_user_no_subscription_is_free():
    user = MagicMock(subscription_tier=None, subscription_status=None)
    assert role_for_user(user) == "Free"


def test_role_for_author_tier():
    """High creator track-record score gets the Author role overlay."""
    user = MagicMock(
        subscription_tier="cook",
        subscription_status="active",
    )
    user.creator_track_record_score = 95  # >= threshold
    role = role_for_user(user, author_threshold=80)
    # Author overlay returned alongside the base; we expose both via list
    assert role == "Pro"  # base remains
    overlay = role_for_user(user, author_threshold=80, include_overlays=True)
    assert "Author" in overlay


def test_sync_role_noop_without_discord_id():
    """sync_role_for_user is a no-op if user has no discord_user_id."""
    user = MagicMock(
        discord_user_id=None,
        subscription_tier="cook",
        subscription_status="active",
    )
    client = MagicMock()
    out = sync_role_for_user(user, client=client)
    assert out["skipped"] == "no_discord_user_id"
    client.assign_role.assert_not_called()


def test_sync_role_assigns_when_token_present():
    """When discord_user_id is set and a client is provided, role is applied."""
    user = MagicMock(
        discord_user_id="123456789012345678",
        subscription_tier="studio",
        subscription_status="active",
    )
    client = MagicMock()
    client.assign_role = MagicMock()
    out = sync_role_for_user(user, client=client)
    assert out["role"] == "All-in"
    client.assign_role.assert_called_once_with("123456789012345678", "All-in")


def test_sync_role_removes_on_cancellation():
    """Cancelled subscriptions downgrade the user back to Free."""
    user = MagicMock(
        discord_user_id="123456789012345678",
        subscription_tier="studio",
        subscription_status="canceled",
    )
    client = MagicMock()
    out = sync_role_for_user(user, client=client)
    assert out["role"] == "Free"
    client.assign_role.assert_called_once_with("123456789012345678", "Free")
