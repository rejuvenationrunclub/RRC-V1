from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from rrc_bot.bot.client import RRCBot
from rrc_bot.bot.commands.rrc import RRCCommands, format_ping


@pytest.mark.parametrize(
    ("database", "expected"),
    [(True, "✅ connected"), (False, "❌ unreachable"), (None, "⚪ not configured")],
)
def test_format_ping_database_states(database: bool | None, expected: str) -> None:
    text = format_ping(0.0874, database, version="9.9.9")
    assert "RRC Bot v9.9.9 is online" in text
    assert "87 ms" in text
    assert f"Database: {expected}" in text


@pytest.mark.parametrize("latency", [math.inf, math.nan, -1.0])
def test_format_ping_before_first_heartbeat(latency: float) -> None:
    assert "measuring" in format_ping(latency, True)


def _fake_interaction(latency: float = 0.05) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(latency=latency),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def _invoke_ping(group: RRCCommands, interaction: SimpleNamespace) -> None:
    command = group.get_command("ping")
    assert isinstance(command, discord.app_commands.Command)
    await command.callback(group, interaction)  # type: ignore[arg-type]


async def test_ping_defers_ephemerally_then_replies_privately() -> None:
    interaction = _fake_interaction(latency=0.123)
    await _invoke_ping(RRCCommands(engine=None), interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert kwargs == {"ephemeral": True}
    assert "123 ms" in args[0]
    assert "not configured" in args[0]


async def test_ping_reports_database_health() -> None:
    sentinel_engine = object()
    group = RRCCommands(engine=sentinel_engine)  # type: ignore[arg-type]
    for healthy, label in ((True, "connected"), (False, "unreachable")):
        interaction = _fake_interaction()
        with patch("rrc_bot.bot.commands.rrc.database_ok", AsyncMock(return_value=healthy)) as ok:
            await _invoke_ping(group, interaction)
        ok.assert_awaited_once_with(sentinel_engine)
        assert label in interaction.followup.send.call_args.args[0]


def test_rrc_group_is_guild_scoped_and_guild_only() -> None:
    bot = RRCBot(guild_id=4242)
    guild_cmds = bot.tree.get_commands(guild=discord.Object(id=4242))
    assert [c.name for c in guild_cmds] == ["rrc"]
    assert bot.tree.get_commands() == []  # nothing registered globally
    group = guild_cmds[0]
    assert isinstance(group, RRCCommands)
    assert group.guild_only is True
    assert [c.name for c in group.commands] == ["ping"]
