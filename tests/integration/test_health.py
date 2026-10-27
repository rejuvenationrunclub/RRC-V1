from __future__ import annotations

import time

import pytest

from rrc_bot.db.health import database_ok
from rrc_bot.db.session import make_engine

pytestmark = pytest.mark.integration


async def test_database_ok_true_for_live_database(test_database_url: str) -> None:
    engine = make_engine(test_database_url)
    try:
        assert await database_ok(engine) is True
    finally:
        await engine.dispose()


async def test_database_ok_false_and_fast_when_unreachable() -> None:
    # Port 1 on localhost: connection refused immediately; never raises.
    engine = make_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    started = time.monotonic()
    try:
        assert await database_ok(engine, deadline_s=2.0) is False
    finally:
        await engine.dispose()
    assert time.monotonic() - started < 3.0
