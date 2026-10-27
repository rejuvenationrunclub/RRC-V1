"""Member-facing ``/rrc`` command group."""

from __future__ import annotations

import math

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine

from rrc_bot import __version__
from rrc_bot.db.health import database_ok


def format_ping(latency_s: float, database: bool | None, version: str = __version__) -> str:
    """Render the /rrc ping reply. ``database`` is None when no database is configured."""
    if math.isfinite(latency_s) and latency_s >= 0:
        latency = f"{round(latency_s * 1000)} ms"
    else:
        latency = "measuring… (just connected)"
    db = {True: "✅ connected", False: "❌ unreachable", None: "⚪ not configured"}[database]
    return f"🏃 Pong! RRC Bot v{version} is online.\n• Discord latency: {latency}\n• Database: {db}"


class RRCCommands(app_commands.Group):
    def __init__(self, *, engine: AsyncEngine | None) -> None:
        super().__init__(name="rrc", description="Rejuvenation Run Club", guild_only=True)
        self._engine = engine

    @app_commands.command(name="ping", description="Check that the RRC bot is online")
    async def ping(self, interaction: discord.Interaction) -> None:
        # Defer first: the DB probe may take up to 2 s and Discord's deadline is 3 s.
        await interaction.response.defer(ephemeral=True, thinking=True)
        database = await database_ok(self._engine) if self._engine is not None else None
        await interaction.followup.send(
            format_ping(interaction.client.latency, database), ephemeral=True
        )
