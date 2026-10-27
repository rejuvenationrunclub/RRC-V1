"""Database liveness probe used by /rrc ping and deployment checks."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)


async def database_ok(engine: AsyncEngine, *, deadline_s: float = 2.0) -> bool:
    """Return True if ``SELECT 1`` succeeds within ``deadline_s`` seconds. Never raises."""
    try:
        async with asyncio.timeout(deadline_s):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        log.warning("Database health check failed", exc_info=True)
        return False
    return True
