"""Transactional outbox (plan §1.2): DMs, channel posts and role changes.

Services write outbox rows in the same transaction as the score change. The
dispatcher delivers them afterwards with retries, so a Discord outage never loses
a score and a crash never sends a notification twice (``dedupe_key`` is unique).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rrc_bot.db.models import Outbox

MAX_ATTEMPTS = 8


@dataclass(frozen=True)
class OutboxItem:
    id: int
    guild_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int


async def enqueue(
    session: AsyncSession, *, guild_id: int, kind: str, dedupe_key: str, payload: dict[str, Any]
) -> None:
    await session.execute(
        insert(Outbox)
        .values(guild_id=guild_id, kind=kind, dedupe_key=dedupe_key, payload=payload)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )


async def dm(session: AsyncSession, *, guild_id: int, user_id: int, content: str, key: str) -> None:
    await enqueue(
        session,
        guild_id=guild_id,
        kind="dm",
        dedupe_key=f"dm:{key}",
        payload={"user_id": user_id, "content": content},
    )


async def post(
    session: AsyncSession, *, guild_id: int, channel_id: int | None, content: str, key: str
) -> None:
    if channel_id is None:
        return
    await enqueue(
        session,
        guild_id=guild_id,
        kind="channel_post",
        dedupe_key=f"post:{key}",
        payload={"channel_id": channel_id, "content": content},
    )


async def add_role(
    session: AsyncSession, *, guild_id: int, user_id: int, role_id: int | None, key: str
) -> None:
    if role_id is None:
        return
    await enqueue(
        session,
        guild_id=guild_id,
        kind="role_add",
        dedupe_key=f"role_add:{key}",
        payload={"user_id": user_id, "role_id": role_id},
    )


async def claim_due(session: AsyncSession, *, now: datetime, limit: int = 20) -> list[OutboxItem]:
    """Mark due rows as ``sending`` and return them. Safe with concurrent dispatchers."""
    rows = await session.execute(
        text(
            """
            UPDATE outbox SET status = 'sending', attempts = attempts + 1
            WHERE id IN (
                SELECT id FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= :now
                ORDER BY id LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, guild_id, kind, payload, attempts
            """
        ),
        {"now": now, "limit": limit},
    )
    items = [OutboxItem(r.id, r.guild_id, r.kind, r.payload, r.attempts) for r in rows]
    return sorted(items, key=lambda i: i.id)


async def mark_sent(session: AsyncSession, item_id: int, *, now: datetime) -> None:
    await session.execute(
        update(Outbox)
        .where(Outbox.id == item_id)
        .values(status="sent", sent_at=now, last_error=None)
    )


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** max(attempts - 1, 0), 3600))


async def mark_failed(
    session: AsyncSession,
    item: OutboxItem,
    *,
    error: str,
    now: datetime,
    permanent: bool,
) -> None:
    give_up = permanent or item.attempts >= MAX_ATTEMPTS
    await session.execute(
        update(Outbox)
        .where(Outbox.id == item.id)
        .values(
            status="failed" if give_up else "pending",
            last_error=error[:500],
            next_attempt_at=now + backoff(item.attempts),
        )
    )


async def release_stuck(session: AsyncSession) -> int:
    """Return rows left in ``sending`` by a crash to the queue (run at startup)."""
    result = await session.execute(
        update(Outbox).where(Outbox.status == "sending").values(status="pending")
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
