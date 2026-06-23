"""Phase D — Discord bot package.

Adam will create the actual Discord server later. Until then,
`start_bot()` returns None when DISCORD_BOT_TOKEN is empty so the
FastAPI lifespan can call it unconditionally.
"""

from app.discord_bot import bot  # noqa: F401
from app.discord_bot.role_sync import role_for_user, sync_role_for_user  # noqa: F401

__all__ = ["bot", "role_for_user", "sync_role_for_user"]
