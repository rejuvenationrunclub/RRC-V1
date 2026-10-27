"""RP and shield ledgers (append-only) with the member row lock (plan §1.4).

``lifetime_rp`` and ``shield_balance`` on ``members`` are caches updated in the same
transaction as the ledger insert. Every entry has a deterministic idempotency key;
posting the same key twice is a no-op that reports the original entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Member, RPLedger, ShieldLedger
from rrc_bot.domain import shields as shield_rules
from rrc_bot.services.common import GuildSettings

THRESHOLD_KINDS = ("rp_threshold_grant", "rp_threshold_forfeit_at_cap")


@dataclass(frozen=True)
class ShieldChange:
    kind: str
    delta: int
    balance_after: int
    rp_threshold: int | None = None


@dataclass(frozen=True)
class Posted:
    created: bool
    amount: int
    lifetime_rp: int
    shield_changes: list[ShieldChange] = field(default_factory=list)


async def lock_member(session: AsyncSession, member_id: int) -> Member:
    """Load the member locked: serializes every RP/shield/attendance write for them.

    ``FOR NO KEY UPDATE`` conflicts with itself (so writers queue up) but not with the
    ``KEY SHARE`` locks that foreign-key checks take. A plain ``FOR UPDATE`` would let two
    transactions that both inserted rows referencing the member deadlock on upgrade.
    Take this lock before writing anything else for the member.
    """
    return (
        await session.execute(
            select(Member)
            .where(Member.id == member_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def post_rp(
    session: AsyncSession,
    *,
    settings: GuildSettings,
    member: Member,
    entry_type: str,
    amount: int,
    idempotency_key: str,
    calc: dict[str, Any],
    source_type: str | None = None,
    source_id: int | None = None,
    cycle_id: int | None = None,
    reason: str | None = None,
    actor_id: int | None = None,
) -> Posted:
    """Append an RP entry for a member locked with ``lock_member``."""
    balance_after = member.lifetime_rp + amount
    entry_id = await session.scalar(
        insert(RPLedger)
        .values(
            guild_id=settings.guild_id,
            member_id=member.id,
            entry_type=entry_type,
            amount=amount,
            balance_after=balance_after,
            source_type=source_type,
            source_id=source_id,
            cycle_id=cycle_id,
            rule_set_id=settings.rule_set_id,
            calc=calc,
            idempotency_key=idempotency_key,
            reason=reason,
            actor_id=actor_id,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(RPLedger.id)
    )
    if entry_id is None:
        return Posted(created=False, amount=0, lifetime_rp=member.lifetime_rp)
    member.lifetime_rp = balance_after
    changes = await _grant_threshold_shields(session, settings=settings, member=member)
    await session.flush()
    return Posted(created=True, amount=amount, lifetime_rp=balance_after, shield_changes=changes)


async def grant_shield(
    session: AsyncSession,
    *,
    member: Member,
    kind: str,
    delta: int,
    idempotency_key: str,
    rp_threshold: int | None = None,
    reason: str | None = None,
    actor_id: int | None = None,
) -> ShieldChange | None:
    """Append a shield entry for a locked member. Returns None if the key was used before."""
    balance_after = member.shield_balance + delta
    entry_id = await session.scalar(
        insert(ShieldLedger)
        .values(
            member_id=member.id,
            kind=kind,
            delta=delta,
            balance_after=balance_after,
            rp_threshold=rp_threshold,
            idempotency_key=idempotency_key,
            reason=reason,
            actor_id=actor_id,
        )
        .on_conflict_do_nothing()
        .returning(ShieldLedger.id)
    )
    if entry_id is None:
        return None
    member.shield_balance = balance_after
    return ShieldChange(kind, delta, balance_after, rp_threshold)


async def _grant_threshold_shields(
    session: AsyncSession, *, settings: GuildSettings, member: Member
) -> list[ShieldChange]:
    """One decision per threshold, ever: grant below the cap, else record a forfeit (Q-S4)."""
    rules = settings.rules.shields
    rows = await session.scalars(
        select(ShieldLedger.rp_threshold).where(
            ShieldLedger.member_id == member.id, ShieldLedger.kind.in_(THRESHOLD_KINDS)
        )
    )
    decided = {t for t in rows.all() if t is not None}
    changes = []
    for threshold in shield_rules.undecided_thresholds(member.lifetime_rp, rules, decided):
        outcome = shield_rules.grant_outcome(member.shield_balance, rules)
        change = await grant_shield(
            session,
            member=member,
            kind="rp_threshold_grant" if outcome == "grant" else "rp_threshold_forfeit_at_cap",
            delta=1 if outcome == "grant" else 0,
            idempotency_key=f"shield:threshold:{member.id}:{threshold}",
            rp_threshold=threshold,
        )
        if change is not None:
            changes.append(change)
    return changes
