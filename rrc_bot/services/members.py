"""Membership: ``/rrc join`` starts the tenure clock (Q-M1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Member, MembershipPeriod, MemberStreakState
from rrc_bot.services import ledger, outbox
from rrc_bot.services.common import GuildSettings, ServiceError


@dataclass(frozen=True)
class JoinResult:
    member_id: int
    created: bool
    enrolled_at: datetime
    shield_balance: int


class NotMember(ServiceError):
    def __init__(self) -> None:
        super().__init__("You haven't joined the RRC program yet. Use `/rrc join` first.")


async def find_member(session: AsyncSession, guild_id: int, discord_user_id: int) -> Member | None:
    stmt = select(Member).where(
        Member.guild_id == guild_id, Member.discord_user_id == discord_user_id
    )
    return (await session.scalars(stmt)).first()


async def require_member(session: AsyncSession, guild_id: int, discord_user_id: int) -> Member:
    member = await find_member(session, guild_id, discord_user_id)
    if member is None or member.status != "active":
        raise NotMember()
    return member


async def join(
    session: AsyncSession, settings: GuildSettings, *, discord_user_id: int, now: datetime
) -> JoinResult:
    """Enroll a member: tenure clock, enrollment shield(s), Initiate role. Idempotent."""
    member_id = await session.scalar(
        insert(Member)
        .values(
            guild_id=settings.guild_id,
            discord_user_id=discord_user_id,
            enrolled_at=now,
            consents={
                "rules_accepted_at": now.isoformat(),
                "rule_set_version": settings.rule_set_version,
            },
        )
        .on_conflict_do_nothing(index_elements=["guild_id", "discord_user_id"])
        .returning(Member.id)
    )
    if member_id is None:
        existing = await find_member(session, settings.guild_id, discord_user_id)
        if existing is None:  # only possible if the row vanished mid-transaction
            raise RuntimeError("member insert conflicted but no member row exists")
        return JoinResult(existing.id, False, existing.enrolled_at, existing.shield_balance)

    member = await ledger.lock_member(session, member_id)
    session.add(MembershipPeriod(member_id=member_id, joined_at=now))
    session.add(MemberStreakState(member_id=member_id, updated_at=now))
    for n in range(settings.rules.shields.enrollment_grant):
        await ledger.grant_shield(
            session,
            member=member,
            kind="enrollment_grant",
            delta=1,
            idempotency_key=f"shield:enroll:{member_id}:{n}",
        )
    await outbox.add_role(
        session,
        guild_id=settings.guild_id,
        user_id=discord_user_id,
        role_id=settings.role_ids.get("initiate"),
        key=f"initiate:{member_id}",
    )
    await session.flush()
    return JoinResult(member_id, True, now, member.shield_balance)
