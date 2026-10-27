"""Migrations apply cleanly, are reversible, and install the append-only guard.

These tests are synchronous on purpose: Alembic's env.py drives its own event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _fetch(url: str, sql: str) -> list[Any]:
    async def go() -> list[Any]:
        conn = await asyncpg.connect(_dsn(url))
        try:
            return list(await conn.fetch(sql))
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.fixture
def migrated(test_database_url: str) -> Iterator[Config]:
    cfg = _alembic_config(test_database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield cfg
    command.downgrade(cfg, "base")


def test_upgrade_installs_extension_and_guard(migrated: Config, test_database_url: str) -> None:
    exts = {r["extname"] for r in _fetch(test_database_url, "SELECT extname FROM pg_extension")}
    assert "btree_gist" in exts
    funcs = _fetch(test_database_url, "SELECT 1 FROM pg_proc WHERE proname = 'rrc_forbid_mutation'")
    assert len(funcs) == 1


def test_append_only_guard_blocks_update_and_delete(
    migrated: Config, test_database_url: str
) -> None:
    async def go() -> None:
        conn = await asyncpg.connect(_dsn(test_database_url))
        try:
            await conn.execute("CREATE TEMP TABLE t_ledger (id int PRIMARY KEY, amount int)")
            await conn.execute(
                "CREATE TRIGGER t_ledger_guard BEFORE UPDATE OR DELETE ON t_ledger "
                "FOR EACH ROW EXECUTE FUNCTION rrc_forbid_mutation()"
            )
            await conn.execute("INSERT INTO t_ledger VALUES (1, 10), (2, -10)")
            with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="append-only"):
                await conn.execute("UPDATE t_ledger SET amount = 99 WHERE id = 1")
            with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="append-only"):
                await conn.execute("DELETE FROM t_ledger WHERE id = 2")
            assert await conn.fetchval("SELECT sum(amount) FROM t_ledger") == 0
        finally:
            await conn.close()

    asyncio.run(go())


def test_downgrade_is_clean(test_database_url: str) -> None:
    cfg = _alembic_config(test_database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    funcs = _fetch(test_database_url, "SELECT 1 FROM pg_proc WHERE proname = 'rrc_forbid_mutation'")
    assert funcs == []


def test_session_timezone_is_utc(test_database_url: str) -> None:
    from rrc_bot.db.session import make_engine

    async def go() -> str:
        engine = make_engine(test_database_url)
        try:
            async with engine.connect() as conn:
                from sqlalchemy import text

                return str((await conn.execute(text("SHOW timezone"))).scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(go()) == "UTC"
