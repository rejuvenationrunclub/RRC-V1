"""Discord client wiring.

Intents are the minimum the plan allows: ``guilds`` plus the privileged
``members`` intent (leave/rejoin detection and rank-role re-application).
Message Content is deliberately never requested.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine

from rrc_bot.bot.commands.admin import AdminCommands
from rrc_bot.bot.commands.rrc import RRCCommands
from rrc_bot.bot.context import BotServices
from rrc_bot.bot.dispatcher import OutboxDispatcher

log = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    return intents


class RRCBot(discord.Client):
    def __init__(self, *, guild_id: int, engine: AsyncEngine | None = None) -> None:
        super().__init__(
            intents=build_intents(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.guild_id = guild_id
        self.engine = engine
        self.services = BotServices(engine)
        self.tree = app_commands.CommandTree(self)
        # Single-server bot: guild-scoped commands update instantly (global ones take up to 1 h).
        guild = discord.Object(id=guild_id)
        self.tree.add_command(RRCCommands(engine=engine, services=self.services), guild=guild)
        self.tree.add_command(AdminCommands(self.services), guild=guild)
        self._dispatcher_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.guild_id)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d application command(s) to guild %s", len(synced), self.guild_id)
        if self.engine is not None:
            dispatcher = OutboxDispatcher(self, self.services)
            self._dispatcher_task = asyncio.create_task(dispatcher.run(), name="outbox")

    async def on_ready(self) -> None:
        user = self.user
        log.info("Connected as %s (id=%s)", user, user.id if user else "?")
        if self.get_guild(self.guild_id) is None:
            log.warning(
                "Bot is not a member of configured guild %s; invite it with the OAuth2 URL "
                "from the README.",
                self.guild_id,
            )

    async def close(self) -> None:
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher_task
        await super().close()
        if self.engine is not None:
            await self.engine.dispose()
