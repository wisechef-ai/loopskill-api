#!/usr/bin/env python3
"""Phase D — one-shot Discord channel structure setup.

Run this AFTER Adam creates the Discord server and the bot has been
invited with `MANAGE_CHANNELS`, `MANAGE_ROLES` and `VIEW_AUDIT_LOG`
permissions.

Channels created (idempotent — skips if a channel of the same name exists):
  - #welcome           (visible to everyone)
  - #announcements     (visible to everyone, write-locked to mods)
  - #general           (visible to everyone)
  - #help              (visible to everyone)
  - #showcase          (visible to everyone)
  - #fleet-support     (visible only to the All-in role)
  - #author-channel    (visible only to the Author role)

Required env:
  DISCORD_BOT_TOKEN   — the bot's token
  DISCORD_GUILD_ID    — the target server's snowflake

Usage:
  $ DISCORD_BOT_TOKEN=… DISCORD_GUILD_ID=… python scripts/setup_discord_channels.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger("setup_discord_channels")


# Channel definitions. `gated_role`=None → public; otherwise the channel is
# only visible to members holding that role (plus admins).
CHANNELS = [
    {"name": "welcome", "gated_role": None},
    {"name": "announcements", "gated_role": None},
    {"name": "general", "gated_role": None},
    {"name": "help", "gated_role": None},
    {"name": "showcase", "gated_role": None},
    {"name": "fleet-support", "gated_role": "All-in"},
    {"name": "author-channel", "gated_role": "Author"},
]


async def _amain(token: str, guild_id: int) -> int:
    try:
        import discord  # type: ignore
    except ImportError:
        print(
            "ERROR: discord.py is not installed. "
            "Install it with `pip install discord.py>=2.3` and re-run.",
            file=sys.stderr,
        )
        return 2

    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():  # noqa: D401
        try:
            guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
            existing = {c.name: c for c in guild.channels}
            roles = {r.name: r for r in guild.roles}

            for spec in CHANNELS:
                name = spec["name"]
                if name in existing:
                    logger.info("✓ #%s already exists, skipping", name)
                    continue

                overwrites = {}
                gated = spec["gated_role"]
                if gated:
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    }
                    role = roles.get(gated)
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(read_messages=True)
                    else:
                        logger.warning(
                            "Role %r not found — channel #%s will be hidden until role exists",
                            gated, name,
                        )

                await guild.create_text_channel(name, overwrites=overwrites)
                logger.info("+ created #%s%s", name, f" (gated: {gated})" if gated else "")
        finally:
            await client.close()

    await client.start(token)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild_id:
        print(
            "ERROR: DISCORD_BOT_TOKEN and DISCORD_GUILD_ID env vars are required",
            file=sys.stderr,
        )
        return 1
    try:
        return asyncio.run(_amain(token, int(guild_id)))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
