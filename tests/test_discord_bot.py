"""Phase 5 — Discord bot role sync tests (tier slug parity update).

The Discord server doesn't exist yet (Adam will create it later). The bot must
no-op gracefully without DISCORD_BOT_TOKEN. When the token is set, role
assignment fires from Stripe webhook events.

Phase 5 update: roles now map to 'Pro+' and 'Pro' (canonical).
Legacy slugs ('studio', 'cook', 'operator') still resolve to correct roles.
Do NOT rename Discord role names — that's a separate ops task.
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


def test_role_for_user_pro_plus():
    """Canonical 'pro_plus' tier → 'Pro+' role."""
    user = MagicMock(subscription_tier="pro_plus", subscription_status="active")
    assert role_for_user(user) == "Pro+"


def test_role_for_user_legacy_studio():
    """Legacy 'studio' slug → still resolves to 'Pro+' (legacy shim)."""
    user = MagicMock(subscription_tier="studio", subscription_status="active")
    assert role_for_user(user) == "Pro+"


def test_role_for_user_legacy_operator():
    """Legacy 'operator' slug → still resolves to 'Pro+' (legacy shim)."""
    user = MagicMock(subscription_tier="operator", subscription_status="active")
    assert role_for_user(user) == "Pro+"


def test_role_for_user_pro():
    """Canonical 'pro' tier → 'Pro' role."""
    user = MagicMock(subscription_tier="pro", subscription_status="active")
    assert role_for_user(user) == "Pro"


def test_role_for_user_legacy_cook():
    """Legacy 'cook' slug → still resolves to 'Pro' (legacy shim)."""
    user = MagicMock(subscription_tier="cook", subscription_status="active")
    assert role_for_user(user) == "Pro"


def test_role_for_user_canceled_falls_back_to_free():
    user = MagicMock(
        subscription_tier="pro_plus", subscription_status="canceled"
    )
    assert role_for_user(user) == "Free"


def test_role_for_user_no_subscription_is_free():
    user = MagicMock(subscription_tier=None, subscription_status=None)
    assert role_for_user(user) == "Free"


def test_role_for_author_tier():
    """High creator track-record score gets the Author role overlay."""
    user = MagicMock(
        subscription_tier="pro",
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
        subscription_tier="pro",
        subscription_status="active",
    )
    client = MagicMock()
    out = sync_role_for_user(user, client=client)
    assert out["skipped"] == "no_discord_user_id"
    client.assign_role.assert_not_called()


def test_sync_role_assigns_pro_plus_when_token_present():
    """When discord_user_id is set and a client is provided, pro_plus → 'Pro+' role."""
    user = MagicMock(
        discord_user_id="123456789012345678",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    client = MagicMock()
    client.assign_role = MagicMock()
    out = sync_role_for_user(user, client=client)
    assert out["role"] == "Pro+"
    client.assign_role.assert_called_once_with("123456789012345678", "Pro+")


def test_sync_role_assigns_legacy_studio_as_pro_plus():
    """Legacy 'studio' tier → 'Pro+' role (backwards compat)."""
    user = MagicMock(
        discord_user_id="123456789012345678",
        subscription_tier="studio",
        subscription_status="active",
    )
    client = MagicMock()
    client.assign_role = MagicMock()
    out = sync_role_for_user(user, client=client)
    assert out["role"] == "Pro+"
    client.assign_role.assert_called_once_with("123456789012345678", "Pro+")


def test_sync_role_removes_on_cancellation():
    """Cancelled subscriptions downgrade the user back to Free."""
    user = MagicMock(
        discord_user_id="123456789012345678",
        subscription_tier="pro_plus",
        subscription_status="canceled",
    )
    client = MagicMock()
    out = sync_role_for_user(user, client=client)
    assert out["role"] == "Free"
    client.assign_role.assert_called_once_with("123456789012345678", "Free")
