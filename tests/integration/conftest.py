"""Fixtures for database tests: a migrated, emptied database and a configured guild."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rrc_bot.db.base import Base
from rrc_bot.db.models import APPEND_ONLY_TABLES  # noqa: F401  (registers the models)
from rrc_bot.db.session import make_engine, make_session_factory
from rrc_bot.services.common import Actor, GuildSettings
from rrc_bot.services.setup import configure_guild

ROOT = Path(__file__).resolve().parents[2]
GUILD_ID = 900_000_000_000_000_001
ADMIN = Actor(discord_user_id=1, capabilities=frozenset({"admin", "staff"}))
STAFF = Actor(discord_user_id=2, capabilities=frozenset({"staff"}))
NOBODY = Actor(discord_user_id=3)
ROLES = {"initiate": 111, "rrc_staff": 222}
CHANNELS = {"admin_log": 333, "upcoming_runs": 444}
ANCHOR = date(2026, 11, 1)  # Sunday; cycle 0 = Nov 1-14 (Phoenix)
NOW = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)  # Sat Nov 7, 7:00 AM Phoenix


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    cfg = alembic_config(test_database_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    eng = make_engine(test_database_url)
    try:
        async with eng.connect() as conn:
            exists = await conn.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
            current = (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                if exists
                else None
            )
        if current != head:
            # Alembic's env.py runs its own event loop; give it a thread.
            await asyncio.to_thread(command.upgrade, cfg, "head")
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with eng.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(engine)


@pytest.fixture
async def guild(sessions: async_sessionmaker[AsyncSession]) -> GuildSettings:
    async with sessions() as s, s.begin():
        return await configure_guild(
            s,
            guild_id=GUILD_ID,
            actor=ADMIN,
            now=NOW,
            anchor_day=ANCHOR,
            roles=ROLES,
            channels=CHANNELS,
        )
