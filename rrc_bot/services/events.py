"""Events: create, publish, open/close check-in, cancel, list, rotating code (plan §3.1)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Cycle, Event, EventType
from rrc_bot.domain import codes
from rrc_bot.domain.rp import resolve_base_rp
from rrc_bot.domain.timeutil import cycle_bounds, cycle_seq, format_local
from rrc_bot.services import audit, outbox
from rrc_bot.services.common import Actor, GuildSettings, ServiceError

CHECKIN_OPENS_BEFORE = timedelta(minutes=30)
CHECKIN_CLOSES_AFTER = timedelta(minutes=30)
MAX_DURATION = timedelta(hours=12)
ACTIVE_STATUSES = ("scheduled", "checkin_open", "checkin_closed")
CODE_STATUSES = ("scheduled", "checkin_open")
_DONE = {
    "publish": "published",
    "open": "opened check-in for",
    "close": "closed check-in for",
    "cancel": "canceled",
}


@dataclass(frozen=True)
class EventView:
    id: int
    title: str
    type_key: str
    type_name: str
    starts_at: datetime
    ends_at: datetime
    location: str | None
    base_rp: int
    status: str
    checkin_opens_at: datetime
    checkin_closes_at: datetime
    group_key: str | None
    cycle_seq: int

    def accepting_codes(self, now: datetime) -> bool:
        return (
            self.status in CODE_STATUSES and self.checkin_opens_at <= now < self.checkin_closes_at
        )


def _view(event: Event, etype: EventType, cycle: Cycle) -> EventView:
    return EventView(
        id=event.id,
        title=event.title,
        type_key=etype.key,
        type_name=etype.name,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        location=event.location,
        base_rp=event.base_rp,
        status=event.status,
        checkin_opens_at=event.checkin_opens_at,
        checkin_closes_at=event.checkin_closes_at,
        group_key=event.activity_group_key,
        cycle_seq=cycle.seq,
    )


def _require_staff(actor: Actor) -> None:
    if not actor.is_staff:
        raise ServiceError("Only RRC staff can do that.")


async def ensure_cycle(
    session: AsyncSession, settings: GuildSettings, seq: int, *, now: datetime
) -> Cycle:
    """The cycle row for ``seq``, created on first use (generation job arrives in Phase 3)."""
    starts_at, ends_at = cycle_bounds(
        seq, settings.anchor_day, settings.rules.cycle_length_days, settings.tz
    )
    status = "upcoming" if now < starts_at else "open" if now < ends_at else "grace"
    await session.execute(
        insert(Cycle)
        .values(
            guild_id=settings.guild_id, seq=seq, starts_at=starts_at, ends_at=ends_at, status=status
        )
        .on_conflict_do_nothing(index_elements=["guild_id", "seq"])
    )
    cycle = await session.scalar(
        select(Cycle).where(Cycle.guild_id == settings.guild_id, Cycle.seq == seq)
    )
    if cycle is None:  # pragma: no cover - the insert above guarantees a row
        raise RuntimeError(f"cycle {seq} missing after insert")
    return cycle


async def load_event(
    session: AsyncSession, settings: GuildSettings, event_id: int, *, lock: bool = False
) -> tuple[Event, EventType, Cycle]:
    stmt = (
        select(Event, EventType, Cycle)
        .join(EventType, EventType.id == Event.event_type_id)
        .join(Cycle, Cycle.id == Event.cycle_id)
        .where(Event.id == event_id, Event.guild_id == settings.guild_id)
    )
    if lock:
        # NO KEY UPDATE: doesn't block the FK checks of concurrent check-in inserts, so
        # finalize (event lock, then member locks) can't deadlock with check-ins.
        stmt = stmt.with_for_update(of=Event, key_share=True)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise ServiceError(f"There's no event #{event_id} in this server.")
    return row[0], row[1], row[2]


async def get_event(session: AsyncSession, settings: GuildSettings, event_id: int) -> EventView:
    return _view(*await load_event(session, settings, event_id))


async def create_event(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    actor: Actor,
    now: datetime,
    type_key: str,
    title: str,
    starts_at: datetime,
    duration: timedelta,
    location: str | None = None,
    base_rp: int | None = None,
    group_key: str | None = None,
    publish: bool = True,
) -> EventView:
    _require_staff(actor)
    title = title.strip()
    if not title or len(title) > 100:
        raise ServiceError("The title must be 1-100 characters.")
    if not timedelta(minutes=5) <= duration <= MAX_DURATION:
        raise ServiceError("An event must last between 5 minutes and 12 hours.")
    etype = await session.scalar(
        select(EventType).where(
            EventType.guild_id == settings.guild_id,
            EventType.key == type_key,
            EventType.active.is_(True),
        )
    )
    if etype is None:
        raise ServiceError(f"Unknown event type '{type_key}'.")
    try:
        rp = resolve_base_rp(
            base_rp,
            default_base_rp=etype.default_base_rp,
            allowed_base_rp=tuple(etype.allowed_base_rp),
        )
    except ValueError as exc:
        raise ServiceError(f"{etype.name}: {exc}.") from exc

    seq = cycle_seq(starts_at, settings.anchor_day, settings.rules.cycle_length_days, settings.tz)
    cycle = await ensure_cycle(session, settings, seq, now=now)
    if cycle.status in ("closing", "closed"):
        raise ServiceError("That date is in a cycle that has already closed.")

    ends_at = starts_at + duration
    event = Event(
        guild_id=settings.guild_id,
        event_type_id=etype.id,
        cycle_id=cycle.id,
        title=title,
        location=(location or "").strip() or None,
        starts_at=starts_at,
        ends_at=ends_at,
        base_rp=rp,
        streak_qualifying=etype.streak_qualifying,
        in_person=etype.in_person,
        activity_group_key=(group_key or "").strip().lower() or None,
        checkin_opens_at=starts_at - CHECKIN_OPENS_BEFORE,
        checkin_closes_at=ends_at + CHECKIN_CLOSES_AFTER,
        verification_mode="code_plus_roster",
        code_secret=os.urandom(32),
        status="scheduled" if publish else "draft",
        created_by=actor.discord_user_id,
    )
    session.add(event)
    await session.flush()
    view = _view(event, etype, cycle)
    when = format_local(starts_at, settings.tz)
    await audit.record(
        session,
        guild_id=settings.guild_id,
        actor=actor,
        action="event.create",
        summary=f"created {etype.name} **{title}** (#{event.id}) for {when}",
        target_type="event",
        target_id=event.id,
        after={"title": title, "starts_at": starts_at, "base_rp": rp, "status": event.status},
        settings=settings,
    )
    if publish:
        await _announce(session, settings, view)
    return view


async def _announce(session: AsyncSession, settings: GuildSettings, view: EventView) -> None:
    where = f" at {view.location}" if view.location else ""
    await outbox.post(
        session,
        guild_id=settings.guild_id,
        channel_id=settings.channel_ids.get("upcoming_runs"),
        content=(
            f"🏃 **{view.title}** ({view.type_name}, {view.base_rp} RP): "
            f"{format_local(view.starts_at, settings.tz)}{where}. "
            "Check in at the run with `/rrc checkin`."
        ),
        key=f"event:{view.id}:published",
    )


async def _transition(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    actor: Actor,
    event_id: int,
    now: datetime,
    action: str,
    allowed_from: tuple[str, ...],
    reason: str | None = None,
) -> EventView:
    event, etype, cycle = await load_event(session, settings, event_id, lock=True)
    if event.status not in allowed_from:
        raise ServiceError(
            f"**{event.title}** is {event.status.replace('_', ' ')}; can't {action} it."
        )
    before = {
        "status": event.status,
        "checkin_opens_at": event.checkin_opens_at,
        "checkin_closes_at": event.checkin_closes_at,
    }
    if action == "publish":
        event.status = "scheduled"
    elif action == "open":
        event.status = "checkin_open"
        event.checkin_opens_at = min(event.checkin_opens_at, now)
        if event.checkin_closes_at <= now:
            event.checkin_closes_at = now + timedelta(hours=1)
    elif action == "close":
        event.status = "checkin_closed"
        event.checkin_closes_at = max(
            min(event.checkin_closes_at, now), event.checkin_opens_at + timedelta(seconds=1)
        )
    elif action == "cancel":
        event.status = "canceled"
        event.cancel_reason = reason
    event.version += 1
    event.updated_at = now
    await session.flush()
    view = _view(event, etype, cycle)
    await audit.record(
        session,
        guild_id=settings.guild_id,
        actor=actor,
        action=f"event.{action}",
        summary=f"{_DONE[action]} **{event.title}** (#{event.id})",
        target_type="event",
        target_id=event.id,
        reason=reason,
        before=before,
        after={
            "status": event.status,
            "checkin_opens_at": event.checkin_opens_at,
            "checkin_closes_at": event.checkin_closes_at,
        },
        settings=settings,
    )
    if action == "publish":
        await _announce(session, settings, view)
    return view


async def publish_event(
    session: AsyncSession, settings: GuildSettings, *, actor: Actor, event_id: int, now: datetime
) -> EventView:
    _require_staff(actor)
    return await _transition(
        session,
        settings,
        actor=actor,
        event_id=event_id,
        now=now,
        action="publish",
        allowed_from=("draft",),
    )


async def open_checkin(
    session: AsyncSession, settings: GuildSettings, *, actor: Actor, event_id: int, now: datetime
) -> EventView:
    _require_staff(actor)
    return await _transition(
        session,
        settings,
        actor=actor,
        event_id=event_id,
        now=now,
        action="open",
        allowed_from=("scheduled", "checkin_closed"),
    )


async def close_checkin(
    session: AsyncSession, settings: GuildSettings, *, actor: Actor, event_id: int, now: datetime
) -> EventView:
    _require_staff(actor)
    return await _transition(
        session,
        settings,
        actor=actor,
        event_id=event_id,
        now=now,
        action="close",
        allowed_from=("scheduled", "checkin_open"),
    )


async def cancel_event(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    actor: Actor,
    event_id: int,
    reason: str,
    now: datetime,
) -> EventView:
    """Admin only (Q-A1). Check-ins already made are kept (Q-E2)."""
    if not actor.is_admin:
        raise ServiceError("Only RRC admins can cancel an event.")
    if not reason.strip():
        raise ServiceError("Please give a reason for canceling.")
    return await _transition(
        session,
        settings,
        actor=actor,
        event_id=event_id,
        now=now,
        action="cancel",
        allowed_from=("draft", *ACTIVE_STATUSES),
        reason=reason.strip(),
    )


async def list_events(
    session: AsyncSession,
    settings: GuildSettings,
    *,
    now: datetime,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
    since: timedelta = timedelta(hours=12),
    limit: int = 25,
) -> list[EventView]:
    """Events ending after ``now - since``, soonest first."""
    rows = await session.execute(
        select(Event, EventType, Cycle)
        .join(EventType, EventType.id == Event.event_type_id)
        .join(Cycle, Cycle.id == Event.cycle_id)
        .where(
            Event.guild_id == settings.guild_id,
            Event.status.in_(statuses),
            Event.ends_at > now - since,
        )
        .order_by(Event.starts_at, Event.id)
        .limit(limit)
    )
    return [_view(e, t, c) for e, t, c in rows.tuples()]


@dataclass(frozen=True)
class CodeView:
    event: EventView
    code: str
    seconds_left: int
    period_s: int


async def show_code(
    session: AsyncSession, settings: GuildSettings, *, actor: Actor, event_id: int, now: datetime
) -> CodeView:
    _require_staff(actor)
    event, etype, cycle = await load_event(session, settings, event_id)
    view = _view(event, etype, cycle)
    if event.status not in CODE_STATUSES:
        raise ServiceError(
            f"**{event.title}** is {event.status.replace('_', ' ')}; check-in is closed."
        )
    return CodeView(
        event=view,
        code=codes.current_code(event.code_secret, now, event.code_period_s),
        seconds_left=codes.seconds_left(now, event.code_period_s),
        period_s=event.code_period_s,
    )
