"""Phase D — Discord bot lifespan + slash command stubs.

If DISCORD_BOT_TOKEN is unset/empty, `start_bot()` is a no-op and logs
"Discord bot disabled (no token)" — this is the expected state until
Adam creates the server.

When the token IS set, `start_bot()` returns an asyncio.Task that runs
the bot client until the FastAPI app shuts down.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from app.config import settings

logger = logging.getLogger("wiserecipes.discord")


def _resolve_token() -> str:
    # Prefer settings (loaded from .env) but allow process env override.
    return os.environ.get("DISCORD_BOT_TOKEN") or settings.DISCORD_BOT_TOKEN or ""


async def start_bot() -> Optional[asyncio.Task]:
    """Start the Discord bot if a token is configured; otherwise no-op.

    Returns the asyncio Task running the bot loop, or None when disabled.
    The FastAPI lifespan handler awaits this on startup and cancels the
    task on shutdown.
    """
    token = _resolve_token()
    if not token:
        logger.info("Discord bot disabled (no token)")
        return None

    try:
        import discord  # type: ignore
    except ImportError:
        logger.warning(
            "Discord bot disabled (discord.py not installed). "
            "Install `discord.py` to enable."
        )
        return None

    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)  # type: ignore

    @client.event
    async def on_ready():  # noqa: D401
        logger.info("Discord bot logged in as %s", client.user)

    task = asyncio.create_task(client.start(token))
    return task


async def stop_bot(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
