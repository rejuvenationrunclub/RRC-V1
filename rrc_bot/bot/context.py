"""Glue between Discord interactions and the services: transactions, actors, replies."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

import discord
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rrc_bot.db.session import make_session_factory
from rrc_bot.services.common import (
    Actor,
    GuildSettings,
    ServiceError,
    capabilities,
    load_settings,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

OOPS = "😬 Something went wrong on our side. Please try again in a minute."


def utcnow() -> datetime:
    return datetime.now(UTC)


class BotServices:
    """Runs service calls in one transaction each, with the guild's settings loaded."""

    def __init__(self, engine: AsyncEngine | None, clock: Callable[[], datetime] = utcnow) -> None:
        self._engine = engine
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        self.clock = clock

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._engine is None:
            raise ServiceError("The bot's database isn't configured.")
        if self._sessions is None:
            self._sessions = make_session_factory(self._engine)
        return self._sessions

    async def run(
        self, guild_id: int, work: Callable[[AsyncSession, GuildSettings], Awaitable[T]]
    ) -> T:
        async with self.sessions() as session, session.begin():
            settings = await load_settings(session, guild_id)
            return await work(session, settings)

    async def run_raw(self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async with self.sessions() as session, session.begin():
            return await work(session)


def actor_for(interaction: discord.Interaction, role_map: dict[str, int]) -> Actor:
    user = interaction.user
    # Guild interactions carry a Member (roles, permissions); anything else gets none.
    roles = getattr(user, "roles", None) or []
    permissions = getattr(user, "guild_permissions", None)
    role_ids = {r.id for r in roles}
    manage = bool(permissions and permissions.manage_guild)
    return Actor(
        discord_user_id=user.id,
        role_ids=tuple(sorted(role_ids)),
        interaction_id=interaction.id,
        capabilities=capabilities(role_ids=role_ids, manage_guild=manage, role_map=role_map),
    )


@dataclass(frozen=True)
class Reply:
    text: str
    view: discord.ui.View | None = None


async def reply(
    interaction: discord.Interaction, work: Callable[[], Awaitable[str | Reply]]
) -> None:
    """Defer privately (Discord's 3-second deadline), run ``work``, send the result."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await safely(work, interaction)
    if isinstance(result, Reply) and result.view is not None:
        await interaction.followup.send(result.text, ephemeral=True, view=result.view)
    else:
        text = result.text if isinstance(result, Reply) else result
        await interaction.followup.send(text, ephemeral=True)


async def safely(
    work: Callable[[], Awaitable[str | Reply]], interaction: discord.Interaction
) -> str | Reply:
    """Run ``work``; user-fixable problems become a ⚠️ message, bugs a generic apology."""
    try:
        return await work()
    except ServiceError as exc:
        return f"⚠️ {exc}"
    except Exception:
        command = interaction.command.qualified_name if interaction.command else "component"
        log.exception("Command %s failed (interaction %s)", command, interaction.id)
        return OOPS


def guild_id_of(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise ServiceError("Use this command inside the RRC server.")
    return interaction.guild_id
