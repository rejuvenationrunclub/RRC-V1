"""Alembic environment (async, PostgreSQL only).

URL resolution order:
1. ``config.attributes["database_url"]`` (set programmatically, e.g. by tests)
2. ``DATABASE_URL`` from the environment / .env via ``Settings``
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import rrc_bot.db.models  # noqa: F401  (registers every table on Base.metadata)
from rrc_bot.config import Settings
from rrc_bot.db.base import Base
from rrc_bot.db.session import make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return str(url)
    return Settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
