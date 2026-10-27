"""Phase 2 services against real PostgreSQL: setup, join, events, check-in, roster, profile."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rrc_bot.db.models import (
    AdminAuditLog,
    Attendance,
    Event,
    Member,
    Outbox,
    RPLedger,
    RuleSetRow,
    ShieldLedger,
)
from rrc_bot.domain import codes
from rrc_bot.services import attendance, checkin, events, members, outbox, profile, setup
from rrc_bot.services.common import GuildSettings, NotSetUp, ServiceError, load_settings
from tests.integration.conftest import ADMIN, ANCHOR, GUILD_ID, NOBODY, NOW, STAFF

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
RUN_START = NOW + timedelta(minutes=10)  # check-in opens 30 min before


async def tx(sessions: Sessions, fn: Any, *args: Any, **kwargs: Any) -> Any:
    async with sessions() as s, s.begin():
        return await fn(s, *args, **kwargs)


async def count(sessions: Sessions, model: Any, *where: Any) -> int:
    async with sessions() as s:
        return int(await s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def make_event(
    sessions: Sessions, guild: GuildSettings, **overrides: Any
) -> events.EventView:
    args: dict[str, Any] = {
        "actor": STAFF,
        "now": NOW,
        "type_key": "flagship",
        "title": "Saturday Flagship",
        "starts_at": RUN_START,
        "duration": timedelta(hours=1),
        "location": "Papago Park",
    }
    args.update(overrides)
    return await tx(sessions, events.create_event, guild, **args)


async def enroll(sessions: Sessions, guild: GuildSettings, user: int) -> None:
    await tx(sessions, members.join, guild, discord_user_id=user, now=NOW - timedelta(days=1))


async def code_for(sessions: Sessions, guild: GuildSettings, event_id: int, at: datetime) -> str:
    view = await tx(sessions, events.show_code, guild, actor=STAFF, event_id=event_id, now=at)
    return str(view.code)


_interaction = iter(range(10_000, 10**9))


async def check_in(
    sessions: Sessions, guild: GuildSettings, user: int, code: str, at: datetime = NOW
) -> checkin.CheckinResult:
    return await tx(
        sessions,
        checkin.checkin,
        guild,
        discord_user_id=user,
        code=code,
        interaction_id=next(_interaction),
        now=at,
    )


# --- setup -------------------------------------------------------------------------------


async def test_setup_seeds_rules_and_event_types(sessions: Sessions, guild: GuildSettings) -> None:
    assert guild.anchor_day == ANCHOR
    assert guild.rule_set_version == 1
    assert "PROPOSED" in guild.rules.label
    assert guild.role_ids == {"initiate": 111, "rrc_staff": 222}
    async with sessions() as s:
        keys = (await s.scalars(text("SELECT key FROM event_types ORDER BY key"))).all()
        assert keys == ["flagship", "popup_run"]


async def test_setup_again_merges_settings_and_keeps_one_rule_set(
    sessions: Sessions, guild: GuildSettings
) -> None:
    updated = await tx(
        sessions,
        setup.configure_guild,
        guild_id=GUILD_ID,
        actor=ADMIN,
        now=NOW,
        roles={"rrc_admin": 555},
    )
    assert updated.role_ids == {"initiate": 111, "rrc_staff": 222, "rrc_admin": 555}
    assert await count(sessions, RuleSetRow) == 1
    assert await count(sessions, AdminAuditLog, AdminAuditLog.action == "guild.setup") == 2


async def test_setup_requires_admin_and_known_keys(
    sessions: Sessions, guild: GuildSettings
) -> None:
    with pytest.raises(ServiceError, match="admins"):
        await tx(sessions, setup.configure_guild, guild_id=GUILD_ID, actor=STAFF, now=NOW)
    with pytest.raises(ServiceError, match="Unknown"):
        await tx(
            sessions,
            setup.configure_guild,
            guild_id=GUILD_ID,
            actor=ADMIN,
            now=NOW,
            roles={"emperor": 1},
        )


async def test_anchor_locked_once_cycles_exist(sessions: Sessions, guild: GuildSettings) -> None:
    await make_event(sessions, guild)
    with pytest.raises(ServiceError, match="cycle start date"):
        await tx(
            sessions,
            setup.configure_guild,
            guild_id=GUILD_ID,
            actor=ADMIN,
            now=NOW,
            anchor_day=ANCHOR + timedelta(days=1),
        )


async def test_commands_before_setup_explain_what_to_do(sessions: Sessions) -> None:
    with pytest.raises(NotSetUp, match="/rrc-admin setup"):
        await tx(sessions, load_settings, GUILD_ID)


# --- join ----------------------------------------------------------------------------------


async def test_join_enrolls_with_shield_and_initiate_role(
    sessions: Sessions, guild: GuildSettings
) -> None:
    result = await tx(sessions, members.join, guild, discord_user_id=42, now=NOW)
    assert result.created and result.shield_balance == 1
    again = await tx(sessions, members.join, guild, discord_user_id=42, now=NOW + timedelta(days=1))
    assert not again.created and again.enrolled_at == NOW and again.member_id == result.member_id
    assert await count(sessions, ShieldLedger) == 1
    async with sessions() as s:
        role = await s.scalar(select(Outbox).where(Outbox.kind == "role_add"))
        assert role is not None and role.payload == {"user_id": 42, "role_id": 111}


async def test_commands_need_membership(sessions: Sessions, guild: GuildSettings) -> None:
    with pytest.raises(members.NotMember, match="/rrc join"):
        await check_in(sessions, guild, 77, "ABCDEF")
    with pytest.raises(members.NotMember):
        await tx(sessions, profile.profile, guild, discord_user_id=77, now=NOW)


# --- events ----------------------------------------------------------------------------------


async def test_create_event_snapshots_rules_and_announces(
    sessions: Sessions, guild: GuildSettings
) -> None:
    view = await make_event(sessions, guild)
    assert (view.base_rp, view.status, view.cycle_seq) == (10, "scheduled", 0)
    assert view.checkin_opens_at == RUN_START - timedelta(minutes=30)
    assert view.checkin_closes_at == RUN_START + timedelta(minutes=90)
    async with sessions() as s:
        posts = (await s.scalars(select(Outbox).where(Outbox.kind == "channel_post"))).all()
    by_channel = {
        p.payload["channel_id"]: p.payload["content"]
        for p in posts
        if "#1" in p.payload["content"] or p.payload["channel_id"] == 444
    }
    assert "created Flagship run **Saturday Flagship** (#1)" in by_channel[333]
    assert "Sat Nov 7, 7:10 AM" in by_channel[444]


async def test_event_validation(sessions: Sessions, guild: GuildSettings) -> None:
    with pytest.raises(ServiceError, match="staff"):
        await make_event(sessions, guild, actor=NOBODY)
    with pytest.raises(ServiceError, match="one of: 3, 5"):
        await make_event(sessions, guild, type_key="popup_run", base_rp=4)
    with pytest.raises(ServiceError, match="one of: 10"):
        await make_event(sessions, guild, base_rp=12)
    with pytest.raises(ServiceError, match="Unknown event type"):
        await make_event(sessions, guild, type_key="yoga")
    with pytest.raises(ServiceError, match="between 5 minutes and 12 hours"):
        await make_event(sessions, guild, duration=timedelta(hours=13))
    popup = await make_event(sessions, guild, type_key="popup_run", base_rp=5, title="Pop-up")
    assert popup.base_rp == 5


async def test_event_in_later_cycle_gets_its_own_cycle(
    sessions: Sessions, guild: GuildSettings
) -> None:
    later = await make_event(sessions, guild, starts_at=RUN_START + timedelta(days=14))
    assert later.cycle_seq == 1


async def test_draft_publish_open_close_cancel(sessions: Sessions, guild: GuildSettings) -> None:
    draft = await make_event(sessions, guild, publish=False)
    assert draft.status == "draft"
    published = await tx(
        sessions, events.publish_event, guild, actor=STAFF, event_id=draft.id, now=NOW
    )
    assert published.status == "scheduled"
    early = NOW - timedelta(hours=2)
    opened = await tx(
        sessions, events.open_checkin, guild, actor=STAFF, event_id=draft.id, now=early
    )
    assert opened.status == "checkin_open" and opened.accepting_codes(early)
    closed = await tx(
        sessions, events.close_checkin, guild, actor=STAFF, event_id=draft.id, now=NOW
    )
    assert closed.status == "checkin_closed" and not closed.accepting_codes(NOW)
    with pytest.raises(ServiceError, match="admins"):
        await tx(
            sessions,
            events.cancel_event,
            guild,
            actor=STAFF,
            event_id=draft.id,
            reason="rain",
            now=NOW,
        )
    canceled = await tx(
        sessions,
        events.cancel_event,
        guild,
        actor=ADMIN,
        event_id=draft.id,
        reason="lightning",
        now=NOW,
    )
    assert canceled.status == "canceled"
    with pytest.raises(ServiceError, match="canceled"):
        await tx(sessions, events.open_checkin, guild, actor=STAFF, event_id=draft.id, now=NOW)


async def test_list_events_soonest_first_and_hides_finished(
    sessions: Sessions, guild: GuildSettings
) -> None:
    b = await make_event(sessions, guild, title="B", starts_at=RUN_START + timedelta(days=2))
    a = await make_event(sessions, guild, title="A")
    old = await make_event(sessions, guild, title="Old", starts_at=NOW - timedelta(days=3))
    listed = await tx(sessions, events.list_events, guild, now=NOW)
    assert [e.id for e in listed] == [a.id, b.id]
    assert old.id not in [e.id for e in listed]


# --- check-in -------------------------------------------------------------------------------


async def test_checkin_happy_path_then_roster_confirms_rp(
    sessions: Sessions, guild: GuildSettings
) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    code = await code_for(sessions, guild, run.id, NOW)

    result = await check_in(sessions, guild, 42, code.lower())
    assert result.outcome == "pending" and result.event is not None and result.event.id == run.id
    assert await count(sessions, RPLedger) == 0  # nothing until staff confirm (Q-V1)
    assert (await check_in(sessions, guild, 42, code)).outcome == "already"

    _, entries = await tx(sessions, attendance.roster, guild, actor=STAFF, event_id=run.id)
    assert [(e.discord_user_id, e.status, e.method) for e in entries] == [(42, "pending", "code")]

    done = await tx(sessions, attendance.finalize, guild, actor=STAFF, event_id=run.id, now=NOW)
    assert (done.verified, done.rp_awarded, done.event.status) == (1, 10, "finalized")
    async with sessions() as s:
        member = await s.scalar(select(Member).where(Member.discord_user_id == 42))
        assert member is not None and member.lifetime_rp == 10
        entry = await s.scalar(select(RPLedger))
        assert entry is not None
        assert (entry.entry_type, entry.amount, entry.balance_after) == ("attendance_base", 10, 10)
        assert entry.rule_set_id == guild.rule_set_id and entry.idempotency_key.startswith("att:")
        dm = await s.scalar(select(Outbox).where(Outbox.kind == "dm"))
        assert dm is not None and "+10 RP" in dm.payload["content"]
    with pytest.raises(ServiceError, match="already confirmed"):
        await tx(sessions, attendance.finalize, guild, actor=STAFF, event_id=run.id, now=NOW)
    # codes stop working once the roster is confirmed
    await enroll(sessions, guild, 43)
    assert (await check_in(sessions, guild, 43, code)).outcome == "bad_code"


async def test_finalize_again_sweeps_late_arrivals(
    sessions: Sessions, guild: GuildSettings
) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    await tx(sessions, attendance.finalize, guild, actor=STAFF, event_id=run.id, now=NOW)
    # a check-in that raced the first finalize is left pending...
    async with sessions() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO attendances (event_id, member_id, status, method, checked_in_at)"
                " VALUES (:e, 1, 'pending', 'code', :t)"
            ),
            {"e": run.id, "t": NOW},
        )
    again = await tx(sessions, attendance.finalize, guild, actor=STAFF, event_id=run.id, now=NOW)
    assert (again.verified, again.rp_awarded) == (1, 10)


async def test_replayed_interaction_is_ignored(sessions: Sessions, guild: GuildSettings) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    code = await code_for(sessions, guild, run.id, NOW)
    kwargs = {"discord_user_id": 42, "code": code, "interaction_id": 555, "now": NOW}
    assert (await tx(sessions, checkin.checkin, guild, **kwargs)).outcome == "pending"
    assert (await tx(sessions, checkin.checkin, guild, **kwargs)).outcome == "replay"
    assert await count(sessions, Attendance) == 1


async def test_codes_only_work_inside_the_window(sessions: Sessions, guild: GuildSettings) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    too_early = run.checkin_opens_at - timedelta(minutes=1)
    secret = await _secret(sessions, run.id)
    assert (
        await check_in(sessions, guild, 42, codes.current_code(secret, too_early, 60), too_early)
    ).outcome == "bad_code"
    too_late = run.checkin_closes_at
    assert (
        await check_in(sessions, guild, 42, codes.current_code(secret, too_late, 60), too_late)
    ).outcome == "bad_code"
    stale = codes.current_code(secret, NOW - timedelta(minutes=3), 60)
    assert (await check_in(sessions, guild, 42, stale)).outcome == "bad_code"


async def _secret(sessions: Sessions, event_id: int) -> bytes:
    async with sessions() as s:
        event = await s.get(Event, event_id)
        assert event is not None
        return event.code_secret


async def test_wrong_codes_are_rate_limited(sessions: Sessions, guild: GuildSettings) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    for _ in range(5):
        assert (await check_in(sessions, guild, 42, "WRONG2")).outcome == "bad_code"
    good = await code_for(sessions, guild, run.id, NOW)
    assert (await check_in(sessions, guild, 42, good)).outcome == "rate_limited"
    later = NOW + timedelta(minutes=11)
    good_later = await code_for(sessions, guild, run.id, later)
    assert (await check_in(sessions, guild, 42, good_later, later)).outcome == "pending"


async def test_shared_activity_group_counts_once(sessions: Sessions, guild: GuildSettings) -> None:
    popup = await make_event(
        sessions, guild, type_key="popup_run", title="Pop-up", group_key="Nov7"
    )
    challenge = await make_event(
        sessions, guild, type_key="popup_run", title="Challenge", group_key="nov7"
    )
    await enroll(sessions, guild, 42)
    assert (
        await check_in(sessions, guild, 42, await code_for(sessions, guild, popup.id, NOW))
    ).outcome == "pending"
    second = await check_in(sessions, guild, 42, await code_for(sessions, guild, challenge.id, NOW))
    assert second.outcome == "group_taken"
    with pytest.raises(ServiceError, match="activity group"):
        await tx(
            sessions,
            attendance.add_manual,
            guild,
            actor=STAFF,
            event_id=challenge.id,
            discord_user_id=42,
            reason=None,
            now=NOW,
        )


# --- roster: reject, manual add, late exceptions ---------------------------------------------


async def test_rejected_checkins_are_not_confirmed(
    sessions: Sessions, guild: GuildSettings
) -> None:
    run = await make_event(sessions, guild)
    for user in (42, 43):
        await enroll(sessions, guild, user)
        await check_in(sessions, guild, user, await code_for(sessions, guild, run.id, NOW))
    await tx(
        sessions,
        attendance.reject,
        guild,
        actor=STAFF,
        event_id=run.id,
        discord_user_id=43,
        reason="not at the run",
        now=NOW,
    )
    done = await tx(sessions, attendance.finalize, guild, actor=STAFF, event_id=run.id, now=NOW)
    assert done.verified == 1
    _, entries = await tx(sessions, attendance.roster, guild, actor=STAFF, event_id=run.id)
    assert {e.discord_user_id: e.status for e in entries} == {42: "verified", 43: "rejected"}
    with pytest.raises(ServiceError, match="already verified"):
        await tx(
            sessions,
            attendance.reject,
            guild,
            actor=STAFF,
            event_id=run.id,
            discord_user_id=42,
            reason="oops",
            now=NOW,
        )


async def test_manual_add_verifies_immediately(sessions: Sessions, guild: GuildSettings) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    result = await tx(
        sessions,
        attendance.add_manual,
        guild,
        actor=STAFF,
        event_id=run.id,
        discord_user_id=42,
        reason=None,
        now=NOW,
    )
    assert (result.newly_verified, result.rp_awarded, result.lifetime_rp) == (True, 10, 10)
    with pytest.raises(ServiceError, match="already confirmed"):
        await tx(
            sessions,
            attendance.add_manual,
            guild,
            actor=STAFF,
            event_id=run.id,
            discord_user_id=42,
            reason=None,
            now=NOW,
        )


async def test_late_exception_limits(sessions: Sessions, guild: GuildSettings) -> None:
    run = await make_event(sessions, guild)
    for user in (42, 43, 44):
        await enroll(sessions, guild, user)
    after_window = run.checkin_closes_at + timedelta(hours=1)
    with pytest.raises(ServiceError, match="reason is required"):
        await tx(
            sessions,
            attendance.add_manual,
            guild,
            actor=STAFF,
            event_id=run.id,
            discord_user_id=42,
            reason=None,
            now=after_window,
        )
    ok = await tx(
        sessions,
        attendance.add_manual,
        guild,
        actor=STAFF,
        event_id=run.id,
        discord_user_id=42,
        reason="phone died",
        now=after_window,
    )
    assert ok.rp_awarded == 10
    # cycle 0 ends Nov 15 00:00 Phoenix; staff get 24h grace, admins 14 days after the event
    past_grace = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    with pytest.raises(ServiceError, match="too late for staff"):
        await tx(
            sessions,
            attendance.add_manual,
            guild,
            actor=STAFF,
            event_id=run.id,
            discord_user_id=43,
            reason="forgot",
            now=past_grace,
        )
    admin_ok = await tx(
        sessions,
        attendance.add_manual,
        guild,
        actor=ADMIN,
        event_id=run.id,
        discord_user_id=43,
        reason="forgot",
        now=past_grace,
    )
    assert admin_ok.newly_verified
    with pytest.raises(ServiceError, match="too late for admins"):
        await tx(
            sessions,
            attendance.add_manual,
            guild,
            actor=ADMIN,
            event_id=run.id,
            discord_user_id=44,
            reason="forgot",
            now=run.ends_at + timedelta(days=15),
        )
    async with sessions() as s:
        methods = (await s.scalars(select(Attendance.method).order_by(Attendance.id))).all()
    assert methods == ["late_exception", "late_exception"]


async def test_canceled_event_keeps_checkins(sessions: Sessions, guild: GuildSettings) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    await check_in(sessions, guild, 42, await code_for(sessions, guild, run.id, NOW))
    await tx(
        sessions, events.cancel_event, guild, actor=ADMIN, event_id=run.id, reason="storm", now=NOW
    )
    _, entries = await tx(sessions, attendance.roster, guild, actor=STAFF, event_id=run.id)
    assert [e.status for e in entries] == ["pending"]


# --- RP, shields, idempotency, concurrency ---------------------------------------------------


async def test_verify_twice_posts_rp_once(sessions: Sessions, guild: GuildSettings) -> None:
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    await check_in(sessions, guild, 42, await code_for(sessions, guild, run.id, NOW))
    async with sessions() as s:
        att_id = await s.scalar(select(Attendance.id))
    first = await tx(sessions, attendance.verify, guild, attendance_id=att_id, actor_id=2, now=NOW)
    second = await tx(sessions, attendance.verify, guild, attendance_id=att_id, actor_id=2, now=NOW)
    assert (first.newly_verified, second.newly_verified) == (True, False)
    assert await count(sessions, RPLedger) == 1


async def test_concurrent_confirmations_post_exactly_once(
    sessions: Sessions, guild: GuildSettings
) -> None:
    """Phase 2 acceptance: one verified attendance -> exactly one base entry under concurrency."""
    run = await make_event(sessions, guild)
    await enroll(sessions, guild, 42)
    await check_in(sessions, guild, 42, await code_for(sessions, guild, run.id, NOW))
    async with sessions() as s:
        att_id = await s.scalar(select(Attendance.id))
    results = await asyncio.gather(
        *(
            tx(sessions, attendance.verify, guild, attendance_id=att_id, actor_id=2, now=NOW)
            for _ in range(12)
        )
    )
    assert sum(r.newly_verified for r in results) == 1
    assert await count(sessions, RPLedger) == 1
    async with sessions() as s:
        assert await s.scalar(select(Member.lifetime_rp)) == 10


async def test_concurrent_checkins_from_many_members(
    sessions: Sessions, guild: GuildSettings
) -> None:
    run = await make_event(sessions, guild)
    users = list(range(100, 112))
    for user in users:
        await enroll(sessions, guild, user)
    code = await code_for(sessions, guild, run.id, NOW)
    results = await asyncio.gather(
        *(check_in(sessions, guild, u, code) for u in users),
        *(check_in(sessions, guild, u, code) for u in users[:4]),  # double taps
    )
    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count("pending") == len(users) and outcomes.count("already") == 4
    done = await tx(sessions, attendance.finalize, guild, actor=STAFF, event_id=run.id, now=NOW)
    assert done.verified == len(users) and done.rp_awarded == 10 * len(users)
    async with sessions() as s:
        total = await s.scalar(select(func.sum(Member.lifetime_rp)))
        ledger_total = await s.scalar(select(func.sum(RPLedger.amount)))
    assert total == ledger_total == 10 * len(users)


async def test_shield_thresholds_grant_then_forfeit_at_cap(
    sessions: Sessions, guild: GuildSettings
) -> None:
    await enroll(sessions, guild, 42)  # 1 enrollment shield
    for n in range(20):  # 20 flagship runs x 10 RP = 200 RP
        run = await make_event(
            sessions, guild, title=f"Run {n}", starts_at=NOW - timedelta(hours=n + 1)
        )
        await tx(
            sessions,
            attendance.add_manual,
            guild,
            actor=ADMIN,
            event_id=run.id,
            discord_user_id=42,
            reason="backfill",
            now=NOW,
        )
    async with sessions() as s:
        member = await s.scalar(select(Member))
        kinds = (
            await s.execute(
                select(
                    ShieldLedger.kind, ShieldLedger.rp_threshold, ShieldLedger.balance_after
                ).order_by(ShieldLedger.id)
            )
        ).all()
    assert member is not None and (member.lifetime_rp, member.shield_balance) == (200, 2)
    assert [tuple(k) for k in kinds] == [
        ("enrollment_grant", None, 1),
        ("rp_threshold_grant", 100, 2),
        ("rp_threshold_forfeit_at_cap", 200, 2),
    ]


async def test_event_rp_is_a_snapshot(sessions: Sessions, guild: GuildSettings) -> None:
    """§5 #15: changing the configured value later never changes existing events or entries."""
    old = await make_event(sessions, guild, title="Old rules")
    async with sessions() as s, s.begin():
        await s.execute(
            text(
                "UPDATE event_types SET default_base_rp = 12, allowed_base_rp = '{12}'"
                " WHERE key = 'flagship'"
            )
        )
    new = await make_event(sessions, guild, title="New rules")
    assert (old.base_rp, new.base_rp) == (10, 12)
    refreshed = await tx(sessions, events.get_event, guild, old.id)
    assert refreshed.base_rp == 10


# --- profile ----------------------------------------------------------------------------------


async def test_profile_shows_progress(sessions: Sessions, guild: GuildSettings) -> None:
    await enroll(sessions, guild, 42)
    run = await make_event(sessions, guild, title="Flagship #1")
    pending_run = await make_event(
        sessions, guild, title="Flagship #2", starts_at=RUN_START + timedelta(hours=2)
    )
    await tx(
        sessions,
        attendance.add_manual,
        guild,
        actor=STAFF,
        event_id=run.id,
        discord_user_id=42,
        reason=None,
        now=NOW,
    )
    await check_in(
        sessions,
        guild,
        42,
        await code_for(sessions, guild, pending_run.id, NOW + timedelta(hours=2)),
        NOW + timedelta(hours=2),
    )
    p = await tx(sessions, profile.profile, guild, discord_user_id=42, now=NOW)
    assert (p.lifetime_rp, p.rank, p.shields, p.shield_cap) == (10, "initiate", 1, 2)
    assert (p.pending_checkins, p.inperson_runs, p.streak_count) == (1, 1, 0)
    assert p.next_rank is not None and p.next_rank.rank == "relentless"
    assert [(c.label, c.have, c.need) for c in p.next_rank.criteria] == [
        ("RP", 10, 150),
        ("weeks as a member", 0, 12),
        ("in-person runs", 1, 4),
    ]
    assert [(r.amount, r.label) for r in p.recent] == [(10, "Flagship #1")]


# --- outbox -------------------------------------------------------------------------------------


async def test_outbox_claim_send_retry_and_give_up(
    sessions: Sessions, guild: GuildSettings
) -> None:
    # Rows become due at the database's real now(); claim "later" so this never depends
    # on today's date.
    later = datetime.now(UTC) + timedelta(days=1)
    async with sessions() as s, s.begin():
        await s.execute(text("DELETE FROM outbox"))
        await outbox.dm(s, guild_id=GUILD_ID, user_id=42, content="hi", key="t1")
        await outbox.dm(s, guild_id=GUILD_ID, user_id=42, content="hi again", key="t1")  # deduped
    async with sessions() as s, s.begin():
        items = await outbox.claim_due(s, now=later)
    assert [i.payload["content"] for i in items] == ["hi"] and items[0].attempts == 1
    async with sessions() as s, s.begin():
        assert await outbox.claim_due(s, now=later) == []  # already claimed
        await outbox.mark_failed(s, items[0], error="discord down", now=later, permanent=False)
    async with sessions() as s, s.begin():
        assert await outbox.claim_due(s, now=later) == []  # backing off
        retry = await outbox.claim_due(s, now=later + timedelta(seconds=6))
        assert [i.attempts for i in retry] == [2]
        await outbox.mark_sent(s, retry[0].id, now=later)
    async with sessions() as s:
        row = await s.scalar(select(Outbox))
        assert row is not None and (row.status, row.last_error) == ("sent", None)

    async with sessions() as s, s.begin():
        await outbox.dm(s, guild_id=GUILD_ID, user_id=43, content="closed DMs", key="t2")
        item = (await outbox.claim_due(s, now=later))[0]
        await outbox.mark_failed(s, item, error="Forbidden", now=later, permanent=True)
        await outbox.dm(s, guild_id=GUILD_ID, user_id=44, content="stuck", key="t3")
        await outbox.claim_due(s, now=later)
        assert await outbox.release_stuck(s) == 1
    async with sessions() as s:
        statuses = dict((await s.execute(select(Outbox.dedupe_key, Outbox.status))).tuples().all())
    assert statuses == {"dm:t1": "sent", "dm:t2": "failed", "dm:t3": "pending"}


async def test_outbox_gives_up_after_max_attempts(sessions: Sessions, guild: GuildSettings) -> None:
    later = datetime.now(UTC) + timedelta(days=1)
    async with sessions() as s, s.begin():
        await s.execute(text("DELETE FROM outbox"))
        await outbox.dm(s, guild_id=GUILD_ID, user_id=42, content="hi", key="t1")
    for attempt in range(outbox.MAX_ATTEMPTS):
        later += timedelta(hours=2)
        async with sessions() as s, s.begin():
            [item] = await outbox.claim_due(s, now=later)
            assert item.attempts == attempt + 1
            await outbox.mark_failed(s, item, error="timeout", now=later, permanent=False)
    async with sessions() as s:
        assert await s.scalar(select(Outbox.status)) == "failed"
