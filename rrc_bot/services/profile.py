"""``/rrc profile``: a member's own numbers and progress toward the next rank."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Attendance, Event, MemberStreakState, ProofSubmission, RPLedger
from rrc_bot.domain import ranks
from rrc_bot.services.common import GuildSettings
from rrc_bot.services.members import require_member


@dataclass(frozen=True)
class RecentEntry:
    at: datetime
    amount: int
    label: str


@dataclass(frozen=True)
class Profile:
    enrolled_at: datetime
    lifetime_rp: int
    rank: str
    shields: int
    shield_cap: int
    streak_count: int
    hot: bool
    activation_cycles: int
    pending_checkins: int
    inperson_runs: int
    next_rank: ranks.RankProgress | None
    recent: list[RecentEntry]


ENTRY_LABELS = {
    "attendance_base": "attendance",
    "streak_bonus": "Hot Streak bonus",
    "streak_bonus_correction": "streak correction",
    "proof": "progress proof",
    "manual_adjustment": "adjustment",
    "reversal": "reversal",
}


async def profile(
    session: AsyncSession, settings: GuildSettings, *, discord_user_id: int, now: datetime
) -> Profile:
    member = await require_member(session, settings.guild_id, discord_user_id)

    inperson_at = list(
        (
            await session.scalars(
                select(Event.starts_at)
                .join(Attendance, Attendance.event_id == Event.id)
                .where(
                    Attendance.member_id == member.id,
                    Attendance.status == "verified",
                    Event.in_person.is_(True),
                )
            )
        ).all()
    )
    proof_days = list(
        (
            await session.scalars(
                select(ProofSubmission.activity_date).where(
                    ProofSubmission.member_id == member.id, ProofSubmission.status == "approved"
                )
            )
        ).all()
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(Attendance)
        .where(Attendance.member_id == member.id, Attendance.status == "pending")
    )
    streak = await session.get(MemberStreakState, member.id)

    facts = ranks.RankFacts(
        enrolled_at=member.enrolled_at,
        lifetime_rp=member.lifetime_rp,
        inperson_at=inperson_at,
        active_weeks=ranks.active_week_count(inperson_at, proof_days, settings.tz),
    )

    rows = await session.execute(
        select(RPLedger.created_at, RPLedger.amount, RPLedger.entry_type, Event.title)
        .outerjoin(
            Attendance,
            (RPLedger.source_type == "attendance") & (Attendance.id == RPLedger.source_id),
        )
        .outerjoin(Event, Event.id == Attendance.event_id)
        .where(RPLedger.member_id == member.id)
        .order_by(RPLedger.id.desc())
        .limit(5)
    )
    recent = [
        RecentEntry(at, amount, title or ENTRY_LABELS.get(kind, kind))
        for at, amount, kind, title in rows.tuples()
    ]

    return Profile(
        enrolled_at=member.enrolled_at,
        lifetime_rp=member.lifetime_rp,
        rank=member.current_rank,
        shields=member.shield_balance,
        shield_cap=settings.rules.shields.max_balance,
        streak_count=streak.count if streak else 0,
        hot=streak.hot if streak else False,
        activation_cycles=settings.rules.streak.activation_cycles,
        pending_checkins=pending or 0,
        inperson_runs=len(inperson_at),
        next_rank=ranks.next_rank(member.current_rank, facts, settings.rules.ranks, now),
        recent=recent,
    )
