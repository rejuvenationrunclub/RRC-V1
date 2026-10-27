"""Member-facing ``/rrc`` command group. Every reply is private (ephemeral)."""

from __future__ import annotations

import math
from datetime import timedelta

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from rrc_bot import __version__
from rrc_bot.bot import render
from rrc_bot.bot.context import BotServices, Reply, guild_id_of, reply, safely
from rrc_bot.db.health import database_ok
from rrc_bot.services import checkin as checkin_service
from rrc_bot.services import events as events_service
from rrc_bot.services import members
from rrc_bot.services import profile as profile_service
from rrc_bot.services.common import GuildSettings


def format_ping(latency_s: float, database: bool | None, version: str = __version__) -> str:
    """Render the /rrc ping reply. ``database`` is None when no database is configured."""
    if math.isfinite(latency_s) and latency_s >= 0:
        latency = f"{round(latency_s * 1000)} ms"
    else:
        latency = "measuring… (just connected)"
    db = {True: "✅ connected", False: "❌ unreachable", None: "⚪ not configured"}[database]
    return f"🏃 Pong! RRC Bot v{version} is online.\n• Discord latency: {latency}\n• Database: {db}"


class JoinView(discord.ui.View):
    """The "I agree" button under the rules and privacy notice (Q-M1)."""

    def __init__(self, services: BotServices, *, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=300)
        self.services = services
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.button(label="I agree, join RRC", style=discord.ButtonStyle.success, emoji="🏃")
    async def agree(
        self, interaction: discord.Interaction, button: discord.ui.Button[JoinView]
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return
        await interaction.response.defer()

        async def work() -> str:
            now = self.services.clock()

            async def go(s: AsyncSession, g: GuildSettings) -> str:
                result = await members.join(s, g, discord_user_id=self.user_id, now=now)
                return render.joined(result, g)

            return await self.services.run(self.guild_id, go)

        text = await safely(work, interaction)
        self.stop()
        await interaction.edit_original_response(
            content=text if isinstance(text, str) else text.text, view=None
        )


class RRCCommands(app_commands.Group):
    def __init__(self, *, engine: AsyncEngine | None, services: BotServices | None = None) -> None:
        super().__init__(name="rrc", description="Rejuvenation Run Club", guild_only=True)
        self._engine = engine
        self.services = services or BotServices(engine)

    @app_commands.command(name="ping", description="Check that the RRC bot is online")
    async def ping(self, interaction: discord.Interaction) -> None:
        # Defer first: the DB probe may take up to 2 s and Discord's deadline is 3 s.
        await interaction.response.defer(ephemeral=True, thinking=True)
        database = await database_ok(self._engine) if self._engine is not None else None
        await interaction.followup.send(
            format_ping(interaction.client.latency, database), ephemeral=True
        )

    @app_commands.command(name="join", description="Join the RRC program (rules and privacy)")
    async def join(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id

        async def work() -> str | Reply:
            guild_id = guild_id_of(interaction)

            async def go(s: AsyncSession, g: GuildSettings) -> str | Reply:
                existing = await members.find_member(s, g.guild_id, user_id)
                if existing is not None:
                    result = members.JoinResult(
                        existing.id, False, existing.enrolled_at, existing.shield_balance
                    )
                    return render.joined(result, g)
                view = JoinView(self.services, guild_id=guild_id, user_id=user_id)
                return Reply(render.join_prompt(g), view)

            return await self.services.run(guild_id, go)

        await reply(interaction, work)

    @app_commands.command(name="events", description="Upcoming runs and this cycle's deadline")
    async def events(self, interaction: discord.Interaction) -> None:
        async def work() -> str:
            now = self.services.clock()

            async def go(s: AsyncSession, g: GuildSettings) -> str:
                upcoming = await events_service.list_events(
                    s, g, now=now, statuses=("scheduled", "checkin_open"), since=timedelta(0)
                )
                return render.events_list(upcoming, g, now)

            return await self.services.run(guild_id_of(interaction), go)

        await reply(interaction, work)

    @app_commands.command(name="checkin", description="Check in at a run with the code staff show")
    @app_commands.describe(code="The 6-character code shown by RRC staff at the run")
    async def checkin(
        self, interaction: discord.Interaction, code: app_commands.Range[str, 1, 20]
    ) -> None:
        async def work() -> str:
            now = self.services.clock()

            async def go(s: AsyncSession, g: GuildSettings) -> str:
                result = await checkin_service.checkin(
                    s,
                    g,
                    discord_user_id=interaction.user.id,
                    code=code,
                    interaction_id=interaction.id,
                    now=now,
                )
                return render.checkin_reply(result, g)

            return await self.services.run(guild_id_of(interaction), go)

        await reply(interaction, work)

    @app_commands.command(name="profile", description="Your RP, rank, shields and progress")
    async def profile(self, interaction: discord.Interaction) -> None:
        async def work() -> str:
            now = self.services.clock()

            async def go(s: AsyncSession, g: GuildSettings) -> str:
                p = await profile_service.profile(
                    s, g, discord_user_id=interaction.user.id, now=now
                )
                return render.profile_text(p, g)

            return await self.services.run(guild_id_of(interaction), go)

        await reply(interaction, work)
