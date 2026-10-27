"""``/rrc checkin CODE`` (plan §3.6, Q-V1).

A valid rotating code creates a *pending* attendance; RP posts when staff confirm
the roster. Replayed interactions are ignored, wrong codes are rate limited, and
the database's unique constraints decide "already checked in".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Attendance, CheckinAttempt, Event, ProcessedInteraction
from rrc_bot.domain import codes
from rrc_bot.services import attendance, ledger
from rrc_bot.services.common import GuildSettings
from rrc_bot.services.events import CODE_STATUSES, EventView, get_event
from rrc_bot.services.members import require_member

FAILURE_WINDOW = timedelta(minutes=10)
MAX_FAILURES = 5

Outcome = Literal[
    "pending", "verified", "already", "group_taken", "bad_code", "rate_limited", "replay"
]


@dataclass(frozen=True)
class CheckinResult:
    outcome: Outcome
    event: EventView | None = None
    rp_awarded: int = 0


async def checkin(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    discord_user_id: int,
    code: str,
    interaction_id: int,
    now: datetime,
) -> CheckinResult:
    member = await require_member(session, settings.guild_id, discord_user_id)
    # Lock first: a member's double taps queue up here instead of racing.
    await ledger.lock_member(session, member.id)

    first_time = await session.scalar(
        insert(ProcessedInteraction)
        .values(interaction_id=interaction_id, member_id=member.id, command="checkin")
        .on_conflict_do_nothing()
        .returning(ProcessedInteraction.interaction_id)
    )
    if first_time is None:
        return CheckinResult("replay")

    failures = await session.scalar(
        select(func.count())
        .select_from(CheckinAttempt)
        .where(
            CheckinAttempt.member_id == member.id,
            CheckinAttempt.success.is_(False),
            CheckinAttempt.attempted_at > now - FAILURE_WINDOW,
        )
    )
    if (failures or 0) >= MAX_FAILURES:
        return CheckinResult("rate_limited")

    candidates = (
        await session.scalars(
            select(Event)
            .where(
                Event.guild_id == settings.guild_id,
                Event.status.in_(CODE_STATUSES),
                Event.checkin_opens_at <= now,
                Event.checkin_closes_at > now,
            )
            .order_by(Event.starts_at, Event.id)
        )
    ).all()
    event = next(
        (e for e in candidates if codes.matches(e.code_secret, code, now, e.code_period_s)), None
    )
    session.add(
        CheckinAttempt(
            member_id=member.id,
            event_id=event.id if event else None,
            attempted_at=now,
            success=event is not None,
        )
    )
    if event is None:
        await session.flush()
        return CheckinResult("bad_code")

    attendance_id = await session.scalar(
        insert(Attendance)
        .values(
            event_id=event.id,
            member_id=member.id,
            status="pending",
            method="code",
            checked_in_at=now,
            source_interaction_id=interaction_id,
            activity_group_key=event.activity_group_key,
        )
        .on_conflict_do_nothing()
        .returning(Attendance.id)
    )
    view = await get_event(session, settings, event.id)
    if attendance_id is None:
        same_event = await session.scalar(
            select(Attendance.id).where(
                Attendance.event_id == event.id, Attendance.member_id == member.id
            )
        )
        return CheckinResult("already" if same_event else "group_taken", view)

    if event.verification_mode == "code":
        result = await attendance.verify(
            session, settings, attendance_id=attendance_id, actor_id=None, now=now
        )
        return CheckinResult("verified", view, result.rp_awarded)
    await session.flush()
    return CheckinResult("pending", view)
