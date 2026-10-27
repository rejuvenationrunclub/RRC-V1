"""Delivers outbox rows to Discord: DMs, channel posts and role changes, with retries."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from rrc_bot.bot.context import BotServices
from rrc_bot.services import outbox
from rrc_bot.services.outbox import OutboxItem

log = logging.getLogger(__name__)


class PermanentFailure(Exception):
    """Retrying won't help (deleted channel or role, DMs closed, member left)."""


class OutboxDispatcher:
    def __init__(self, client: discord.Client, services: BotServices, *, interval_s: float = 5.0):
        self.client = client
        self.services = services
        self.interval_s = interval_s

    async def run(self) -> None:
        await self.client.wait_until_ready()
        released = await self.services.run_raw(outbox.release_stuck)
        if released:
            log.info("Requeued %d outbox item(s) left mid-delivery", released)
        while not self.client.is_closed():
            try:
                delivered = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Outbox drain failed; retrying shortly")
                delivered = 0
            await asyncio.sleep(0 if delivered else self.interval_s)

    async def drain_once(self) -> int:
        now = self.services.clock()
        items = await self.services.run_raw(lambda s: outbox.claim_due(s, now=now))
        for item in items:
            try:
                await self.deliver(item)
            except (PermanentFailure, discord.Forbidden, discord.NotFound) as exc:
                log.warning("Outbox %s (%s) failed permanently: %s", item.id, item.kind, exc)
                await self._fail(item, exc, permanent=True)
            except Exception as exc:
                log.warning("Outbox %s (%s) failed, will retry: %s", item.id, item.kind, exc)
                await self._fail(item, exc, permanent=False)
            else:
                await self._sent(item)
        return len(items)

    async def _sent(self, item: OutboxItem) -> None:
        at = self.services.clock()
        await self.services.run_raw(lambda s: outbox.mark_sent(s, item.id, now=at))

    async def _fail(self, item: OutboxItem, exc: Exception, *, permanent: bool) -> None:
        at = self.services.clock()
        error = f"{type(exc).__name__}: {exc}"
        await self.services.run_raw(
            lambda s: outbox.mark_failed(s, item, error=error, now=at, permanent=permanent)
        )

    async def deliver(self, item: OutboxItem) -> None:
        p: dict[str, Any] = item.payload
        if item.kind == "dm":
            user = self.client.get_user(p["user_id"]) or await self.client.fetch_user(p["user_id"])
            await user.send(p["content"])
        elif item.kind == "channel_post":
            channel = self.client.get_channel(p["channel_id"]) or await self.client.fetch_channel(
                p["channel_id"]
            )
            if not isinstance(channel, discord.abc.Messageable):
                raise PermanentFailure(f"channel {p['channel_id']} can't receive messages")
            await channel.send(p["content"])
        elif item.kind in ("role_add", "role_remove"):
            guild = self.client.get_guild(item.guild_id)
            if guild is None:
                raise PermanentFailure(f"not in guild {item.guild_id}")
            role = guild.get_role(p["role_id"])
            if role is None:
                raise PermanentFailure(f"role {p['role_id']} no longer exists")
            member = guild.get_member(p["user_id"]) or await guild.fetch_member(p["user_id"])
            if item.kind == "role_add":
                await member.add_roles(role, reason="RRC bot")
            else:
                await member.remove_roles(role, reason="RRC bot")
        else:
            raise PermanentFailure(f"unknown outbox kind {item.kind!r}")
