"""Attendance: staff roster, confirmation (verify), rejection and manual adds (plan §3.2).

Verifying an attendance posts its base RP exactly once (idempotency key
``att:{id}:base``) under the member row lock, plus any shield grants it earns.
The Hot Streak bonus needs closed cycles, which arrive in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Attendance, Event, Member
from rrc_bot.domain.timeutil import format_local
from rrc_bot.services import audit, ledger, outbox
from rrc_bot.services.common import Actor, GuildSettings, ServiceError
from rrc_bot.services.events import EventView, _view, load_event
from rrc_bot.services.members import require_member

ADMIN_LATE_LIMIT = timedelta(days=14)  # Q-E4


@dataclass(frozen=True)
class RosterEntry:
    attendance_id: int
    discord_user_id: int | None
    status: str
    method: str
    checked_in_at: datetime
    is_late: bool


@dataclass(frozen=True)
class VerifyResult:
    attendance_id: int
    discord_user_id: int | None
    newly_verified: bool
    rp_awarded: int
    lifetime_rp: int
    shields_gained: int


def _require_staff(actor: Actor) -> None:
    if not actor.is_staff:
        raise ServiceError("Only RRC staff can do that.")


async def roster(
    session: AsyncSession, settings: GuildSettings, *, actor: Actor, event_id: int
) -> tuple[EventView, list[RosterEntry]]:
    _require_staff(actor)
    event, etype, cycle = await load_event(session, settings, event_id)
    rows = await session.execute(
        select(Attendance, Member.discord_user_id)
        .join(Member, Member.id == Attendance.member_id)
        .where(Attendance.event_id == event_id)
        .order_by(Attendance.checked_in_at, Attendance.id)
    )
    entries = [
        RosterEntry(a.id, uid, a.status, a.method, a.checked_in_at, a.is_late)
        for a, uid in rows.tuples()
    ]
    return _view(event, etype, cycle), entries


async def verify(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    attendance_id: int,
    actor_id: int | None,
    now: datetime,
) -> VerifyResult:
    """Confirm one attendance and post its base RP. Safe to call twice or concurrently."""
    member_id = await session.scalar(
        select(Attendance.member_id).where(Attendance.id == attendance_id)
    )
    if member_id is None:
        raise ServiceError("That check-in no longer exists.")
    member = await ledger.lock_member(session, member_id)  # serializes per member
    att = (
        await session.execute(
            select(Attendance)
            .where(Attendance.id == attendance_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    event = await session.get(Event, att.event_id)
    if event is None or event.guild_id != settings.guild_id:
        raise ServiceError("That check-in belongs to another server.")
    if att.status == "verified":
        return VerifyResult(att.id, member.discord_user_id, False, 0, member.lifetime_rp, 0)
    if att.status not in ("pending", "rejected"):
        raise ServiceError(f"That check-in is {att.status}; it can't be confirmed.")

    att.status = "verified"
    att.verified_at = now
    att.verified_by = actor_id
    await session.flush()

    posted = ledger.Posted(created=False, amount=0, lifetime_rp=member.lifetime_rp)
    if event.base_rp > 0:
        posted = await ledger.post_rp(
            session,
            settings=settings,
            member=member,
            entry_type="attendance_base",
            amount=event.base_rp,
            idempotency_key=f"att:{att.id}:base",
            calc={"base": event.base_rp, "event_id": event.id, "source": "event_snapshot"},
            source_type="attendance",
            source_id=att.id,
            cycle_id=event.cycle_id,
            actor_id=actor_id,
        )
    gained = sum(c.delta for c in posted.shield_changes)
    if member.discord_user_id is not None:
        lines = [
            f"✅ Your attendance at **{event.title}** "
            f"({format_local(event.starts_at, settings.tz)}) is confirmed: "
            f"**+{posted.amount} RP**. Lifetime total: **{posted.lifetime_rp} RP**."
        ]
        if gained:
            lines.append(f"🛡️ You earned a shield! You now have {member.shield_balance}.")
        await outbox.dm(
            session,
            guild_id=settings.guild_id,
            user_id=member.discord_user_id,
            content="\n".join(lines),
            key=f"att:{att.id}:verified",
        )
    return VerifyResult(
        att.id, member.discord_user_id, True, posted.amount, posted.lifetime_rp, gained
    )


@dataclass(frozen=True)
class FinalizeResult:
    event: EventView
    verified: int
    rp_awarded: int


async def finalize(
    session: AsyncSession, settings: GuildSettings, *, actor: Actor, event_id: int, now: datetime
) -> FinalizeResult:
    """Staff confirm the roster: every pending check-in becomes verified, then the event closes.

    Running it again on a finalized event confirms any check-in that landed while the
    first run was in progress.
    """
    _require_staff(actor)
    event, etype, cycle = await load_event(session, settings, event_id, lock=True)
    if event.status not in ("scheduled", "checkin_open", "checkin_closed", "finalized"):
        raise ServiceError(
            f"**{event.title}** is {event.status.replace('_', ' ')}; there's nothing to confirm."
        )
    pending = (
        await session.scalars(
            select(Attendance.id)
            .join(Member, Member.id == Attendance.member_id)
            .where(Attendance.event_id == event_id, Attendance.status == "pending")
            .order_by(Member.id)  # consistent lock order across members
        )
    ).all()
    if event.status == "finalized" and not pending:
        raise ServiceError(f"Everyone who checked in to **{event.title}** is already confirmed.")
    verified = rp = 0
    for attendance_id in pending:
        result = await verify(
            session, settings, attendance_id=attendance_id, actor_id=actor.discord_user_id, now=now
        )
        verified += int(result.newly_verified)
        rp += result.rp_awarded
    event.status = "finalized"
    if event.checkin_closes_at > now:
        event.checkin_closes_at = max(now, event.checkin_opens_at + timedelta(seconds=1))
    event.version += 1
    event.updated_at = now
    await session.flush()
    await audit.record(
        session,
        guild_id=settings.guild_id,
        actor=actor,
        action="event.finalize",
        summary=f"confirmed the roster for **{event.title}** (#{event.id}): "
        f"{verified} attendance(s), {rp} RP",
        target_type="event",
        target_id=event.id,
        after={"verified": verified, "rp_awarded": rp},
        settings=settings,
    )
    return FinalizeResult(_view(event, etype, cycle), verified, rp)


async def _attendance_for(
    session: AsyncSession, *, event_id: int, member_id: int
) -> Attendance | None:
    stmt = select(Attendance).where(
        Attendance.event_id == event_id, Attendance.member_id == member_id
    )
    return (await session.scalars(stmt)).first()


async def reject(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    actor: Actor,
    event_id: int,
    discord_user_id: int,
    reason: str,
    now: datetime,
) -> None:
    """Reject a pending check-in (e.g. someone who wasn't actually there)."""
    _require_staff(actor)
    if not reason.strip():
        raise ServiceError("Please give a reason.")
    event, _, _ = await load_event(session, settings, event_id)
    member = await require_member(session, settings.guild_id, discord_user_id)
    await ledger.lock_member(session, member.id)
    att = await _attendance_for(session, event_id=event_id, member_id=member.id)
    if att is None:
        raise ServiceError(f"<@{discord_user_id}> didn't check in to **{event.title}**.")
    if att.status != "pending":
        raise ServiceError(
            f"That check-in is already {att.status}. Undoing a confirmed check-in needs an "
            "admin reversal, which arrives in a later update."
        )
    att.status = "rejected"
    att.reason = reason.strip()
    att.verified_by = actor.discord_user_id
    await session.flush()
    await audit.record(
        session,
        guild_id=settings.guild_id,
        actor=actor,
        action="attendance.reject",
        summary=f"rejected <@{discord_user_id}>'s check-in for **{event.title}** (#{event.id})",
        target_type="attendance",
        target_id=att.id,
        reason=reason.strip(),
        settings=settings,
    )


async def add_manual(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    actor: Actor,
    event_id: int,
    discord_user_id: int,
    reason: str | None,
    now: datetime,
) -> VerifyResult:
    """Staff add someone who was there but couldn't check in. Verified immediately.

    After the check-in window this is a late exception (Q-E4): staff until the cycle's
    grace period ends, admins up to 14 days after the event, reason required.
    """
    _require_staff(actor)
    event, _, cycle = await load_event(session, settings, event_id)
    if event.status in ("draft", "canceled"):
        raise ServiceError(f"**{event.title}** is {event.status}; attendance can't be added.")
    late = now >= event.checkin_closes_at
    reason = (reason or "").strip() or None
    if late:
        if reason is None:
            raise ServiceError("Check-in has closed, so a reason is required for a late add.")
        staff_limit = cycle.ends_at + timedelta(
            hours=settings.rules.streak.late_checkin_grace_hours
        )
        limit = (
            max(staff_limit, event.ends_at + ADMIN_LATE_LIMIT) if actor.is_admin else staff_limit
        )
        if now > limit:
            who = "admins" if actor.is_admin else "staff"
            raise ServiceError(f"It's too late for {who} to add attendance to **{event.title}**.")

    member = await require_member(session, settings.guild_id, discord_user_id)
    await ledger.lock_member(session, member.id)
    attendance_id = await session.scalar(
        insert(Attendance)
        .values(
            event_id=event.id,
            member_id=member.id,
            status="pending",
            method="late_exception" if late else "staff_manual",
            checked_in_at=now,
            is_late=late,
            reason=reason,
            source_interaction_id=actor.interaction_id,
            activity_group_key=event.activity_group_key,
        )
        .on_conflict_do_nothing()
        .returning(Attendance.id)
    )
    if attendance_id is None:
        existing = await _attendance_for(session, event_id=event.id, member_id=member.id)
        if existing is None:
            raise ServiceError(
                f"<@{discord_user_id}> already has attendance for this activity group "
                f"(`{event.activity_group_key}`)."
            )
        if existing.status == "verified":
            raise ServiceError(f"<@{discord_user_id}> is already confirmed for **{event.title}**.")
        if existing.status == "rejected" and not actor.is_admin:
            raise ServiceError("That check-in was rejected; only an admin can override it.")
        attendance_id = existing.id
    result = await verify(
        session, settings, attendance_id=attendance_id, actor_id=actor.discord_user_id, now=now
    )
    await audit.record(
        session,
        guild_id=settings.guild_id,
        actor=actor,
        action="attendance.add",
        summary=f"added <@{discord_user_id}> to **{event.title}** (#{event.id})"
        + (" as a late exception" if late else ""),
        target_type="attendance",
        target_id=attendance_id,
        reason=reason,
        settings=settings,
    )
    return result
