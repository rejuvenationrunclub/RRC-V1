"""Staff and admin ``/rrc-admin`` commands (plan §4.2).

Discord hides the group from members without Manage Server (``default_permissions``);
give staff roles access under Server Settings > Integrations. The bot re-checks
capabilities on every call regardless, so the Discord setting is only a convenience.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.bot import render
from rrc_bot.bot.context import BotServices, Reply, actor_for, guild_id_of, reply, safely
from rrc_bot.bot.parsing import parse_clock, parse_day
from rrc_bot.db.models import GuildConfig
from rrc_bot.domain.timeutil import PHOENIX, local_datetime
from rrc_bot.services import attendance, events, setup
from rrc_bot.services.common import Actor, GuildSettings, ServiceError

StaffWork = Callable[[AsyncSession, GuildSettings, Actor, datetime], Awaitable["str | Reply"]]


async def event_choices(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Autocomplete for every ``event`` option: recent and upcoming events, newest first."""
    services: BotServices | None = getattr(interaction.client, "services", None)
    if services is None or interaction.guild_id is None:
        return []
    now = services.clock()
    guild_id = interaction.guild_id

    async def go(s: AsyncSession, g: GuildSettings) -> list[app_commands.Choice[int]]:
        if not actor_for(interaction, g.role_ids).is_staff:
            return []  # members get no event names, even if they can see the command
        found = await events.list_events(
            s,
            g,
            now=now,
            statuses=("draft", "scheduled", "checkin_open", "checkin_closed", "finalized"),
            since=timedelta(days=14),
            limit=100,
        )
        needle = current.strip().lower().lstrip("#")
        matches = [e for e in found if needle in f"{e.id} {e.title}".lower()]
        return [
            app_commands.Choice(name=render.event_choice(e, g.tz), value=e.id) for e in matches[:25]
        ]

    try:
        return await services.run(guild_id, go)
    except Exception:
        return []


class CodeRefreshView(discord.ui.View):
    """A Refresh button under the check-in code so staff can keep showing a fresh one."""

    def __init__(self, services: BotServices, *, guild_id: int, event_id: int) -> None:
        super().__init__(timeout=30 * 60)
        self.services = services
        self.guild_id = guild_id
        self.event_id = event_id

    @discord.ui.button(label="Refresh code", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button[CodeRefreshView]
    ) -> None:
        await interaction.response.defer()

        async def work() -> str:
            now = self.services.clock()

            async def go(s: AsyncSession, g: GuildSettings) -> str:
                actor = actor_for(interaction, g.role_ids)
                code = await events.show_code(s, g, actor=actor, event_id=self.event_id, now=now)
                return render.code_text(code, g, now)

            return await self.services.run(self.guild_id, go)

        text = await safely(work, interaction)
        await interaction.edit_original_response(
            content=text if isinstance(text, str) else text.text
        )


def setup_warnings(guild: Any, settings: GuildSettings) -> list[str]:
    """Self-check of bot permissions and role order (plan §6 step 6)."""
    me = getattr(guild, "me", None)
    if me is None:
        return ["I can't see my own member record; is the bot in this server?"]
    warnings = []
    initiate_id = settings.role_ids.get("initiate")
    if initiate_id:
        role = guild.get_role(initiate_id)
        if role is None:
            warnings.append(
                "The Initiate role no longer exists; pick another with `/rrc-admin setup`."
            )
        else:
            if not me.guild_permissions.manage_roles:
                warnings.append(
                    "I need the **Manage Roles** permission to give out the Initiate role."
                )
            if role >= me.top_role:
                warnings.append(
                    "Drag my role **above** the Initiate role (Server Settings > Roles); "
                    "Discord only lets bots assign roles below their own."
                )
    else:
        warnings.append("No Initiate role set, so new members won't get one.")
    for key, label in (("admin_log", "admin log"), ("upcoming_runs", "upcoming runs")):
        channel_id = settings.channel_ids.get(key)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel_id and channel is None:
            warnings.append(f"The {label} channel no longer exists.")
        elif channel is not None:
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.send_messages):
                warnings.append(
                    f"I can't post in {channel.mention} ({label}); allow View and Send."
                )
    return warnings


class AdminCommands(app_commands.Group):
    event = app_commands.Group(name="event", description="Create and run RRC events")
    attendance = app_commands.Group(name="attendance", description="Fix attendance records")

    def __init__(self, services: BotServices) -> None:
        super().__init__(
            name="rrc-admin",
            description="RRC staff and admin tools",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )
        self.services = services

    async def _staff(self, interaction: discord.Interaction, work: StaffWork) -> None:
        async def run() -> str | Reply:
            now = self.services.clock()

            async def go(s: AsyncSession, g: GuildSettings) -> str | Reply:
                return await work(s, g, actor_for(interaction, g.role_ids), now)

            return await self.services.run(guild_id_of(interaction), go)

        await reply(interaction, run)

    # --- setup -----------------------------------------------------------------------------

    @app_commands.command(name="setup", description="Configure the RRC bot (server managers)")
    @app_commands.describe(
        cycle_start=(
            "First day of cycle 1, e.g. 2026-11-01 (default: today). Locked once runs exist."
        ),
        admin_role="Role whose members are RRC admins",
        staff_role="Role whose members are RRC staff (create runs, show codes, confirm rosters)",
        initiate_role="Role given to everyone who joins",
        admin_log="Private channel for the bot's admin log",
        upcoming_runs="Channel where new runs are announced",
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        cycle_start: str | None = None,
        admin_role: discord.Role | None = None,
        staff_role: discord.Role | None = None,
        initiate_role: discord.Role | None = None,
        admin_log: discord.TextChannel | None = None,
        upcoming_runs: discord.TextChannel | None = None,
    ) -> None:
        async def run() -> str:
            guild_id = guild_id_of(interaction)
            now = self.services.clock()
            roles = {
                k: r.id
                for k, r in (
                    ("rrc_admin", admin_role),
                    ("rrc_staff", staff_role),
                    ("initiate", initiate_role),
                )
                if r is not None
            }
            channels = {
                k: c.id
                for k, c in (("admin_log", admin_log), ("upcoming_runs", upcoming_runs))
                if c is not None
            }

            async def go(s: AsyncSession) -> GuildSettings:
                existing = await s.scalar(
                    select(GuildConfig.role_ids).where(GuildConfig.guild_id == guild_id)
                )
                actor = actor_for(interaction, dict(existing or {}))
                anchor = None
                if cycle_start:
                    anchor = parse_day(cycle_start, now=now, tz=PHOENIX)
                return await setup.configure_guild(
                    s,
                    guild_id=guild_id,
                    actor=actor,
                    now=now,
                    anchor_day=anchor,
                    roles=roles,
                    channels=channels,
                )

            settings = await self.services.run_raw(go)
            return render.setup_text(settings, setup_warnings(interaction.guild, settings))

        await reply(interaction, run)

    # --- events ----------------------------------------------------------------------------

    @event.command(name="create", description="Schedule a run (times are Phoenix time)")
    @app_commands.describe(
        kind="Flagship or pop-up",
        title="What members will see, e.g. Saturday Flagship",
        day="Date: 2026-11-07, 11/7, today or tomorrow",
        start="Start time: 7:00 AM, 7am or 19:30",
        duration_minutes="How long the run lasts (default 60)",
        location="Where to meet",
        points="Pop-ups only: 3 or 5 RP",
        group="Optional: events with the same group count as one activity",
        publish="Announce it now (default yes); no saves a draft",
    )
    @app_commands.rename(day="date")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Flagship run", value="flagship"),
            app_commands.Choice(name="Pop-up run", value="popup_run"),
        ]
    )
    async def event_create(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        title: app_commands.Range[str, 1, 100],
        day: str,
        start: str,
        duration_minutes: app_commands.Range[int, 5, 720] = 60,
        location: app_commands.Range[str, 1, 100] | None = None,
        points: int | None = None,
        group: app_commands.Range[str, 1, 50] | None = None,
        publish: bool = True,
    ) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            starts_at = local_datetime(parse_day(day, now=now, tz=g.tz), parse_clock(start), g.tz)
            view = await events.create_event(
                s,
                g,
                actor=actor,
                now=now,
                type_key=kind.value,
                title=title,
                starts_at=starts_at,
                duration=timedelta(minutes=duration_minutes),
                location=location,
                base_rp=points,
                group_key=group,
                publish=publish,
            )
            return render.event_created(view, g)

        await self._staff(interaction, work)

    @event.command(name="list", description="Recent and upcoming events")
    async def event_list(self, interaction: discord.Interaction) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            if not actor.is_staff:
                raise ServiceError("Only RRC staff can do that.")
            found = await events.list_events(
                s,
                g,
                now=now,
                statuses=("draft", "scheduled", "checkin_open", "checkin_closed", "finalized"),
                since=timedelta(days=7),
            )
            if not found:
                return (
                    "No events in the last week or coming up. "
                    "Create one with `/rrc-admin event create`."
                )
            return render.clip(
                "\n".join(
                    f"• #{e.id} {render.event_line(e, g.tz, now)} · {e.status}" for e in found
                )
            )

        await self._staff(interaction, work)

    @event.command(name="code", description="Show the rotating check-in code for a run")
    @app_commands.describe(event="Which event")
    @app_commands.autocomplete(event=event_choices)
    async def event_code(self, interaction: discord.Interaction, event: int) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> Reply:
            code = await events.show_code(s, g, actor=actor, event_id=event, now=now)
            view = CodeRefreshView(self.services, guild_id=g.guild_id, event_id=event)
            return Reply(render.code_text(code, g, now), view)

        await self._staff(interaction, work)

    @event.command(name="open", description="Open check-in now (or reopen it)")
    @app_commands.autocomplete(event=event_choices)
    async def event_open(self, interaction: discord.Interaction, event: int) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            view = await events.open_checkin(s, g, actor=actor, event_id=event, now=now)
            return render.event_status(view, g, now)

        await self._staff(interaction, work)

    @event.command(name="close", description="Close check-in now")
    @app_commands.autocomplete(event=event_choices)
    async def event_close(self, interaction: discord.Interaction, event: int) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            view = await events.close_checkin(s, g, actor=actor, event_id=event, now=now)
            return render.event_status(view, g, now)

        await self._staff(interaction, work)

    @event.command(name="publish", description="Publish a draft event")
    @app_commands.autocomplete(event=event_choices)
    async def event_publish(self, interaction: discord.Interaction, event: int) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            view = await events.publish_event(s, g, actor=actor, event_id=event, now=now)
            return render.event_status(view, g, now)

        await self._staff(interaction, work)

    @event.command(name="roster", description="Who checked in, and who is waiting for confirmation")
    @app_commands.autocomplete(event=event_choices)
    async def event_roster(self, interaction: discord.Interaction, event: int) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            view, entries = await attendance.roster(s, g, actor=actor, event_id=event)
            return render.roster_text(view, entries, g)

        await self._staff(interaction, work)

    @event.command(name="finalize", description="Confirm everyone waiting on the roster (posts RP)")
    @app_commands.autocomplete(event=event_choices)
    async def event_finalize(self, interaction: discord.Interaction, event: int) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            return render.finalized(
                await attendance.finalize(s, g, actor=actor, event_id=event, now=now)
            )

        await self._staff(interaction, work)

    @event.command(name="cancel", description="Cancel an event (admins; check-ins are kept)")
    @app_commands.autocomplete(event=event_choices)
    async def event_cancel(
        self, interaction: discord.Interaction, event: int, reason: app_commands.Range[str, 3, 200]
    ) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            view = await events.cancel_event(
                s, g, actor=actor, event_id=event, reason=reason, now=now
            )
            return render.event_status(view, g, now)

        await self._staff(interaction, work)

    # --- attendance ------------------------------------------------------------------------

    @attendance.command(
        name="add", description="Add someone who was at the run (confirmed at once)"
    )
    @app_commands.describe(reason="Required after check-in has closed")
    @app_commands.autocomplete(event=event_choices)
    async def attendance_add(
        self,
        interaction: discord.Interaction,
        event: int,
        member: discord.Member,
        reason: app_commands.Range[str, 3, 200] | None = None,
    ) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            result = await attendance.add_manual(
                s, g, actor=actor, event_id=event, discord_user_id=member.id, reason=reason, now=now
            )
            return render.added(result)

        await self._staff(interaction, work)

    @attendance.command(
        name="reject", description="Reject a check-in from someone who wasn't there"
    )
    @app_commands.autocomplete(event=event_choices)
    async def attendance_reject(
        self,
        interaction: discord.Interaction,
        event: int,
        member: discord.Member,
        reason: app_commands.Range[str, 3, 200],
    ) -> None:
        async def work(s: AsyncSession, g: GuildSettings, actor: Actor, now: datetime) -> str:
            await attendance.reject(
                s, g, actor=actor, event_id=event, discord_user_id=member.id, reason=reason, now=now
            )
            return f"❌ Rejected <@{member.id}>'s check-in. They won't get RP for it."

        await self._staff(interaction, work)
