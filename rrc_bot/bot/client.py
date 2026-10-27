"""Discord client wiring.

Intents are the minimum the plan allows: ``guilds`` plus the privileged
``members`` intent (leave/rejoin detection and rank-role re-application).
Message Content is deliberately never requested.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    return intents


class RRCBot(discord.Client):
    def __init__(self, *, guild_id: int) -> None:
        super().__init__(
            intents=build_intents(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Single-server bot: guild-scoped commands update instantly.
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d application command(s) to guild %s", len(synced), self.guild_id)

    async def on_ready(self) -> None:
        user = self.user
        log.info("Connected as %s (id=%s)", user, user.id if user else "?")
        if self.get_guild(self.guild_id) is None:
            log.warning(
                "Bot is not a member of configured guild %s; invite it with the OAuth2 URL "
                "from the README.",
                self.guild_id,
            )
