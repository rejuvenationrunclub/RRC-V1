"""End-to-end: slash-command callbacks with fake Discord interactions, real database.

This walks the test-server script: setup -> create a run -> show the code -> a member
joins and checks in -> staff review the roster and confirm -> the member's profile,
and the outbox dispatcher delivering the DM, role and posts.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rrc_bot.bot.commands.admin import AdminCommands, CodeRefreshView, event_choices
from rrc_bot.bot.commands.rrc import JoinView, RRCCommands
from rrc_bot.bot.context import BotServices
from rrc_bot.bot.dispatcher import OutboxDispatcher
from rrc_bot.db.models import Outbox
from rrc_bot.domain.timeutil import PHOENIX, format_local
from rrc_bot.services import outbox
from tests.integration.conftest import GUILD_ID, NOW

pytestmark = pytest.mark.integration

OWNER, STAFFER, RUNNER = 1001, 1002, 1003
STAFF_ROLE, INITIATE_ROLE = 2002, 2003
_ids = itertools.count(5_000_000)


class FakeChannel(discord.abc.Messageable):
    def __init__(self) -> None:
        self.send = AsyncMock()


def interaction(
    services: BotServices, user_id: int, *, manage: bool = False, roles: tuple[int, ...] = ()
) -> Any:
    user = SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=r) for r in roles],
        guild_permissions=SimpleNamespace(manage_guild=manage),
    )
    return SimpleNamespace(
        id=next(_ids),
        user=user,
        guild_id=GUILD_ID,
        guild=None,
        command=None,
        client=SimpleNamespace(services=services, latency=0.05),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def sent(i: Any) -> tuple[str, Any]:
    """The text (and view) of the single private follow-up a command sent."""
    i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    i.followup.send.assert_awaited_once()
    args, kwargs = i.followup.send.call_args
    assert kwargs["ephemeral"] is True
    return args[0], kwargs.get("view")


def sub(group: app_commands.Group, path: str) -> Any:
    node: Any = group
    for name in path.split():
        node = node.get_command(name)
    return node


@pytest.fixture
def services(engine: AsyncEngine) -> BotServices:
    clock = {"now": NOW}
    svc = BotServices(engine, clock=lambda: clock["now"])
    svc.clock_state = clock  # type: ignore[attr-defined]
    return svc


async def test_full_test_server_walkthrough(services: BotServices) -> None:
    member_cmds = RRCCommands(engine=None, services=services)
    admin = AdminCommands(services)

    # Before setup, members are told what to do.
    i = interaction(services, RUNNER)
    await sub(member_cmds, "events").callback(member_cmds, i)
    assert "/rrc-admin setup" in sent(i)[0]

    # A non-manager can't run setup; the server owner can.
    i = interaction(services, STAFFER)
    await sub(admin, "setup").callback(admin, i)
    assert "Only RRC admins" in sent(i)[0]
    i = interaction(services, OWNER, manage=True)
    await sub(admin, "setup").callback(
        admin,
        i,
        cycle_start="2026-11-01",
        staff_role=SimpleNamespace(id=STAFF_ROLE),
        initiate_role=SimpleNamespace(id=INITIATE_ROLE),
        admin_log=SimpleNamespace(id=3003),
        upcoming_runs=SimpleNamespace(id=3004),
    )
    text, _ = sent(i)
    assert "Cycle 1 starts: Sun Nov 1, 12:00 AM" in text and f"<@&{STAFF_ROLE}>" in text

    # Staff (by role) create a run starting in 10 minutes.
    i = interaction(services, STAFFER, roles=(STAFF_ROLE,))
    await sub(admin, "event create").callback(
        admin,
        i,
        kind=app_commands.Choice(name="Flagship run", value="flagship"),
        title="Saturday Flagship",
        day="today",
        start="7:10am",
        location="Papago Park",
    )
    text, _ = sent(i)
    assert "Flagship run **Saturday Flagship** (#1) published" in text

    # Autocomplete finds it.
    choices = await event_choices(interaction(services, STAFFER, roles=(STAFF_ROLE,)), "sat")
    assert [c.value for c in choices] == [1] and choices[0].name.startswith("#1 Saturday Flagship")
    assert await event_choices(interaction(services, RUNNER), "sat") == []  # members: nothing

    # Members see it, but can't use staff commands.
    i = interaction(services, RUNNER)
    await sub(member_cmds, "events").callback(member_cmds, i)
    assert "Saturday Flagship" in sent(i)[0]
    i = interaction(services, RUNNER)
    await sub(admin, "event code").callback(admin, i, event=1)
    assert "Only RRC staff" in sent(i)[0]

    # Staff show the code, with a Refresh button.
    i = interaction(services, STAFFER, roles=(STAFF_ROLE,))
    await sub(admin, "event code").callback(admin, i, event=1)
    text, view = sent(i)
    assert isinstance(view, CodeRefreshView)
    code = text.split("`")[1]
    refresh = interaction(services, STAFFER, roles=(STAFF_ROLE,))
    await view.refresh.callback(refresh)
    assert code in refresh.edit_original_response.call_args.kwargs["content"]

    # The runner can't check in before joining; then joins via the agree button.
    i = interaction(services, RUNNER)
    await sub(member_cmds, "checkin").callback(member_cmds, i, code=code)
    assert "/rrc join" in sent(i)[0]
    i = interaction(services, RUNNER)
    await sub(member_cmds, "join").callback(member_cmds, i)
    text, join_view = sent(i)
    assert "Privacy" in text and isinstance(join_view, JoinView)
    stranger = interaction(services, STAFFER)
    await join_view.agree.callback(stranger)
    stranger.response.send_message.assert_awaited_once()
    click = interaction(services, RUNNER)
    await join_view.agree.callback(click)
    assert "Welcome to RRC" in click.edit_original_response.call_args.kwargs["content"]

    # Check in (twice: the second says already).
    i = interaction(services, RUNNER)
    await sub(member_cmds, "checkin").callback(member_cmds, i, code=code.lower())
    assert "You're checked in to **Saturday Flagship**" in sent(i)[0]
    i = interaction(services, RUNNER)
    await sub(member_cmds, "checkin").callback(member_cmds, i, code=code)
    assert "already checked in" in sent(i)[0]

    # Staff review and confirm the roster.
    i = interaction(services, STAFFER, roles=(STAFF_ROLE,))
    await sub(admin, "event roster").callback(admin, i, event=1)
    text, _ = sent(i)
    assert f"Waiting for confirmation (1)\n• <@{RUNNER}>" in text
    i = interaction(services, STAFFER, roles=(STAFF_ROLE,))
    await sub(admin, "event finalize").callback(admin, i, event=1)
    assert "1 member(s), 10 RP awarded" in sent(i)[0]

    # The runner's profile.
    i = interaction(services, RUNNER)
    await sub(member_cmds, "profile").callback(member_cmds, i)
    text, _ = sent(i)
    assert "Lifetime: **10 RP**" in text and "+10 RP · Saturday Flagship" in text

    # Everything queued for Discord is delivered by the dispatcher.
    user = SimpleNamespace(send=AsyncMock())
    channels = {3003: FakeChannel(), 3004: FakeChannel()}
    role = SimpleNamespace(id=INITIATE_ROLE)
    member = SimpleNamespace(add_roles=AsyncMock())
    guild = SimpleNamespace(
        get_role=lambda rid: role if rid == INITIATE_ROLE else None,
        get_member=lambda uid: member if uid == RUNNER else None,
    )
    client = SimpleNamespace(
        get_user=lambda uid: user if uid == RUNNER else None,
        get_channel=channels.get,
        get_guild=lambda gid: guild if gid == GUILD_ID else None,
    )
    services.clock_state["now"] = datetime.now(UTC) + timedelta(days=1)  # type: ignore[attr-defined]
    dispatcher = OutboxDispatcher(client, services)  # type: ignore[arg-type]
    delivered = await dispatcher.drain_once()
    assert delivered >= 4
    assert "+10 RP" in user.send.call_args.args[0]
    member.add_roles.assert_awaited_once_with(role, reason="RRC bot")
    announcement = channels[3004].send.call_args.args[0]
    assert "Saturday Flagship" in announcement
    assert format_local(NOW + timedelta(minutes=10), PHOENIX) in announcement
    assert channels[3003].send.await_count >= 3  # setup, create, finalize audit lines
    async with services.sessions() as s:
        statuses = set((await s.scalars(select(Outbox.status))).all())
    assert statuses == {"sent"}


async def test_unexpected_errors_get_a_friendly_reply(services: BotServices) -> None:
    member_cmds = RRCCommands(engine=None, services=services)

    async def boom(*_: Any) -> Any:
        raise RuntimeError("database exploded")

    services.run = boom  # type: ignore[method-assign]
    i = interaction(services, RUNNER)
    await sub(member_cmds, "profile").callback(member_cmds, i)
    text, _ = sent(i)
    assert text.startswith("😬") and "exploded" not in text


async def test_dispatcher_permanent_and_retryable_failures(services: BotServices) -> None:
    async with services.sessions() as s, s.begin():
        await outbox.dm(s, guild_id=GUILD_ID, user_id=1, content="closed DMs", key="a")
        await outbox.dm(s, guild_id=GUILD_ID, user_id=2, content="flaky", key="b")
        await outbox.enqueue(
            s,
            guild_id=GUILD_ID,
            kind="role_add",
            dedupe_key="c",
            payload={"user_id": 1, "role_id": 9},
        )

    forbidden = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user"
    )
    users = {
        1: SimpleNamespace(send=AsyncMock(side_effect=forbidden)),
        2: SimpleNamespace(send=AsyncMock(side_effect=TimeoutError("slow"))),
    }
    client = SimpleNamespace(get_user=users.get, get_guild=lambda _: None)
    services.clock_state["now"] = datetime.now(UTC) + timedelta(days=1)  # type: ignore[attr-defined]
    dispatcher = OutboxDispatcher(client, services)  # type: ignore[arg-type]
    assert await dispatcher.drain_once() == 3
    async with services.sessions() as s:
        rows = {
            r.dedupe_key: (r.status, r.last_error) for r in (await s.scalars(select(Outbox))).all()
        }
    assert rows["dm:a"][0] == "failed" and "Forbidden" in (rows["dm:a"][1] or "")
    assert rows["dm:b"][0] == "pending" and "slow" in (rows["dm:b"][1] or "")
    assert rows["c"][0] == "failed" and "not in guild" in (rows["c"][1] or "")
