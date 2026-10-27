"""Shared service types: user-facing errors, the acting user, and guild settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import GuildConfig, RuleSetRow
from rrc_bot.domain.rules import RuleSet
from rrc_bot.domain.timeutil import local_date

ROLE_KEYS = ("initiate", "relentless", "legacy", "hot_streak", "rrc_admin", "rrc_staff")
CHANNEL_KEYS = ("admin_log", "upcoming_runs")


class ServiceError(Exception):
    """A problem the user can fix. The message is safe to show in Discord."""


class NotSetUp(ServiceError):
    def __init__(self) -> None:
        super().__init__(
            "The RRC bot isn't set up in this server yet. A server admin needs to run "
            "`/rrc-admin setup` first."
        )


@dataclass(frozen=True)
class Actor:
    """Who is acting, for authorization and the audit log."""

    discord_user_id: int
    role_ids: tuple[int, ...] = ()
    interaction_id: int | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.capabilities

    @property
    def is_staff(self) -> bool:
        return "staff" in self.capabilities or self.is_admin


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    tz: ZoneInfo
    anchor_day: date
    role_ids: dict[str, int]
    channel_ids: dict[str, int]
    rule_set_id: int
    rule_set_version: int
    rules: RuleSet


def settings_from_rows(config: GuildConfig, rule_set: RuleSetRow) -> GuildSettings:
    tz = ZoneInfo(config.display_tz)
    return GuildSettings(
        guild_id=config.guild_id,
        tz=tz,
        anchor_day=local_date(config.cycle_anchor_utc, tz),
        role_ids={k: int(v) for k, v in config.role_ids.items()},
        channel_ids={k: int(v) for k, v in config.channel_ids.items()},
        rule_set_id=rule_set.id,
        rule_set_version=rule_set.version,
        rules=RuleSet.model_validate(rule_set.config),
    )


async def load_settings(session: AsyncSession, guild_id: int) -> GuildSettings:
    """Settings plus the active rule set. Raises ``NotSetUp`` before ``/rrc-admin setup``."""
    row = (
        await session.execute(
            select(GuildConfig, RuleSetRow)
            .join(RuleSetRow, RuleSetRow.guild_id == GuildConfig.guild_id)
            .where(GuildConfig.guild_id == guild_id, RuleSetRow.status == "active")
        )
    ).one_or_none()
    if row is None:
        raise NotSetUp()
    return settings_from_rows(row[0], row[1])


def capabilities(
    *, role_ids: set[int], manage_guild: bool, role_map: dict[str, int]
) -> frozenset[str]:
    """Map Discord roles to bot capabilities (plan §4.2).

    Server managers are always admins, so the server owner can bootstrap setup.
    """
    caps: set[str] = set()
    if manage_guild or role_map.get("rrc_admin") in role_ids:
        caps.update(("admin", "staff"))
    if role_map.get("rrc_staff") in role_ids:
        caps.add("staff")
    return frozenset(caps)


def jsonable(value: Any) -> Any:
    """Make audit snapshots JSON-safe (datetimes and dates become ISO strings)."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
