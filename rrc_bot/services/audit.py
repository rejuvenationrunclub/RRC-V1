"""Admin audit log (append-only) plus a one-line summary to #rrc-admin-log."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import AdminAuditLog
from rrc_bot.services import outbox
from rrc_bot.services.common import Actor, GuildSettings, jsonable


async def record(
    session: AsyncSession,
    *,
    guild_id: int,
    actor: Actor,
    action: str,
    summary: str,
    target_type: str | None = None,
    target_id: int | None = None,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    settings: GuildSettings | None = None,
) -> int:
    row = AdminAuditLog(
        guild_id=guild_id,
        actor_id=actor.discord_user_id,
        actor_role_ids=list(actor.role_ids),
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        before=jsonable(before) if before is not None else None,
        after=jsonable(after) if after is not None else None,
        interaction_id=actor.interaction_id,
    )
    session.add(row)
    await session.flush()
    if settings is not None:
        line = f"📝 <@{actor.discord_user_id}> {summary}"
        if reason:
            line += f" (reason: {reason})"
        await outbox.post(
            session,
            guild_id=guild_id,
            channel_id=settings.channel_ids.get("admin_log"),
            content=line,
            key=f"audit:{row.id}",
        )
    return row.id
