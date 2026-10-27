"""``/rrc-admin setup``: guild configuration, the first rule set and event types."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Cycle, EventType, GuildConfig, RuleSetRow
from rrc_bot.domain.rp import event_type_specs
from rrc_bot.domain.rules import default_rule_set
from rrc_bot.domain.timeutil import PHOENIX, local_date, local_midnight
from rrc_bot.services import audit
from rrc_bot.services.common import (
    CHANNEL_KEYS,
    ROLE_KEYS,
    Actor,
    GuildSettings,
    ServiceError,
    load_settings,
)


async def configure_guild(
    session: AsyncSession,
    *,
    guild_id: int,
    actor: Actor,
    now: datetime,
    anchor_day: date | None = None,
    roles: dict[str, int] | None = None,
    channels: dict[str, int] | None = None,
) -> GuildSettings:
    """Create or update the guild's settings. Only keys given are changed.

    The first run also activates rule set v1 (the PROPOSED defaults) and seeds the
    event types. The cycle anchor defaults to today in Phoenix on the first run.
    """
    if not actor.is_admin:
        raise ServiceError("Only RRC admins (or server managers) can run setup.")
    roles = roles or {}
    channels = channels or {}
    if unknown := (set(roles) - set(ROLE_KEYS)) | (set(channels) - set(CHANNEL_KEYS)):
        raise ServiceError(f"Unknown setting(s): {', '.join(sorted(unknown))}")

    config = await session.get(GuildConfig, guild_id, with_for_update=True)
    before = None
    if config is None:
        config = GuildConfig(
            guild_id=guild_id,
            display_tz=PHOENIX.key,
            cycle_anchor_utc=local_midnight(anchor_day or local_date(now, PHOENIX), PHOENIX),
            role_ids={},
            channel_ids={},
        )
        session.add(config)
        await session.flush()
    else:
        before = {
            "cycle_anchor_utc": config.cycle_anchor_utc,
            "role_ids": dict(config.role_ids),
            "channel_ids": dict(config.channel_ids),
        }
        if anchor_day is not None:
            if await _has_cycles(session, guild_id):
                raise ServiceError(
                    "The cycle start date can't change once runs have been scheduled."
                )
            config.cycle_anchor_utc = local_midnight(anchor_day, PHOENIX)
    config.role_ids = {**config.role_ids, **roles}
    config.channel_ids = {**config.channel_ids, **channels}
    config.updated_at = now

    rules = default_rule_set()
    has_rules = await session.scalar(
        select(RuleSetRow.id).where(RuleSetRow.guild_id == guild_id).limit(1)
    )
    if has_rules is None:
        session.add(
            RuleSetRow(
                guild_id=guild_id,
                version=1,
                status="active",
                config=rules.to_json(),
                label=rules.label,
                effective_from=now,
                created_by=actor.discord_user_id,
            )
        )
    for spec in event_type_specs(rules):
        await session.execute(
            insert(EventType)
            .values(
                guild_id=guild_id,
                key=spec.key,
                name=spec.name,
                default_base_rp=spec.default_base_rp,
                allowed_base_rp=list(spec.allowed_base_rp),
                streak_qualifying=spec.streak_qualifying,
                in_person=spec.in_person,
            )
            .on_conflict_do_nothing(index_elements=["guild_id", "key"])
        )
    await session.flush()

    settings = await load_settings(session, guild_id)
    await audit.record(
        session,
        guild_id=guild_id,
        actor=actor,
        action="guild.setup",
        summary="updated the RRC bot setup",
        before=before,
        after={
            "cycle_anchor_utc": config.cycle_anchor_utc,
            "role_ids": config.role_ids,
            "channel_ids": config.channel_ids,
        },
        settings=settings,
    )
    return settings


async def _has_cycles(session: AsyncSession, guild_id: int) -> bool:
    return (
        await session.scalar(select(Cycle.id).where(Cycle.guild_id == guild_id).limit(1))
    ) is not None
