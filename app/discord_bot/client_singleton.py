"""Process-global accessor for the Discord role client.

The bot lifespan stamps a `DiscordRoleClient` instance here on startup;
webhook handlers call `get_role_client()` synchronously without having to
await the bot. Returns None when the bot isn't running (token absent),
making `_maybe_sync_discord_role` a graceful no-op.
"""
from __future__ import annotations

from typing import Any, Optional

_client: Optional[Any] = None


def set_role_client(client: Any | None) -> None:
    global _client
    _client = client


def get_role_client() -> Any | None:
    return _client
