"""The migrated schema matches the models and enforces the plan's safety rules in SQL."""

from __future__ import annotations

from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rrc_bot.db.base import Base
from rrc_bot.db.models import APPEND_ONLY_TABLES
from rrc_bot.services.common import GuildSettings
from rrc_bot.services.members import join
from tests.integration.conftest import GUILD_ID, NOW

pytestmark = pytest.mark.integration


async def test_models_and_migrations_agree(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    assert diff == []


async def test_append_only_triggers_installed(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT tgrelid::regclass::text FROM pg_trigger "
                "WHERE tgname LIKE '%_append_only' AND NOT tgisinternal"
            )
        )
        assert sorted(r[0] for r in rows) == sorted(APPEND_ONLY_TABLES)


async def _ledger_row(sessions: async_sessionmaker[AsyncSession], guild: GuildSettings) -> int:
    async with sessions() as s, s.begin():
        await join(s, guild, discord_user_id=42, now=NOW)
        return int(
            await s.scalar(
                text(
                    "INSERT INTO rp_ledger (guild_id, member_id, entry_type, amount, balance_after,"
                    " rule_set_id, calc, idempotency_key) VALUES (:g, 1, 'manual_adjustment', 5, 5,"
                    " :r, '{}', 'test:1') RETURNING id"
                ),
                {"g": GUILD_ID, "r": guild.rule_set_id},
            )
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE rp_ledger SET amount = 500",
        "DELETE FROM rp_ledger",
        "UPDATE shield_ledger SET delta = 0",
        "DELETE FROM shield_ledger",
        "DELETE FROM admin_audit_log",
    ],
)
async def test_ledgers_reject_update_and_delete(
    sessions: async_sessionmaker[AsyncSession], guild: GuildSettings, statement: str
) -> None:
    await _ledger_row(sessions, guild)
    async with sessions() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            async with s.begin():
                await s.execute(text(statement))


async def test_ledger_idempotency_key_and_reversal_are_unique(
    sessions: async_sessionmaker[AsyncSession], guild: GuildSettings
) -> None:
    entry = await _ledger_row(sessions, guild)
    insert = (
        "INSERT INTO rp_ledger (guild_id, member_id, entry_type, amount, balance_after,"
        " rule_set_id, calc, idempotency_key, reverses_entry_id) VALUES"
        " (:g, 1, 'reversal', -5, 0, :r, '{}', :k, :rev)"
    )
    params = {"g": GUILD_ID, "r": guild.rule_set_id, "rev": entry}
    async with sessions() as s:
        with pytest.raises(IntegrityError):
            async with s.begin():
                await s.execute(text(insert), {**params, "k": "test:1", "rev": None})
    async with sessions() as s, s.begin():
        await s.execute(text(insert), {**params, "k": "rev:1"})
    async with sessions() as s:
        with pytest.raises(IntegrityError, match="reverses_entry_id"):
            async with s.begin():
                await s.execute(text(insert), {**params, "k": "rev:2"})


async def test_active_rule_set_is_frozen(
    sessions: async_sessionmaker[AsyncSession], guild: GuildSettings
) -> None:
    async with sessions() as s:
        with pytest.raises(DBAPIError, match="cannot be changed"):
            async with s.begin():
                await s.execute(text("UPDATE rule_sets SET config = '{}'"))
        with pytest.raises(DBAPIError, match="cannot be changed"):
            async with s.begin():
                await s.execute(text("DELETE FROM rule_sets"))
    # retiring is the one allowed change; a second active version is blocked by the index
    async with sessions() as s:
        with pytest.raises(IntegrityError, match="one_active"):
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO rule_sets (guild_id, version, status, config, label)"
                        " VALUES (:g, 2, 'active', '{}', 'v2')"
                    ),
                    {"g": GUILD_ID},
                )
    async with sessions() as s, s.begin():
        await s.execute(text("UPDATE rule_sets SET status = 'retired'"))
        await s.execute(
            text(
                "INSERT INTO rule_sets (guild_id, version, status, config, label)"
                " VALUES (:g, 2, 'draft', '{}', 'v2')"
            ),
            {"g": GUILD_ID},
        )
        await s.execute(text("UPDATE rule_sets SET label = 'v2 edited' WHERE status = 'draft'"))


async def test_cycles_cannot_overlap(
    sessions: async_sessionmaker[AsyncSession], guild: GuildSettings
) -> None:
    insert = (
        "INSERT INTO cycles (guild_id, seq, starts_at, ends_at, status)"
        " VALUES (:g, :seq, :s, :e, 'open')"
    )
    async with sessions() as s, s.begin():
        await s.execute(
            text(insert), {"g": GUILD_ID, "seq": 0, "s": NOW, "e": NOW + timedelta(days=14)}
        )
    async with sessions() as s:
        with pytest.raises(IntegrityError, match="no_overlap"):
            async with s.begin():
                await s.execute(
                    text(insert),
                    {
                        "g": GUILD_ID,
                        "seq": 1,
                        "s": NOW + timedelta(days=13),
                        "e": NOW + timedelta(days=27),
                    },
                )
    async with sessions() as s, s.begin():  # touching end-to-start is fine (half-open ranges)
        await s.execute(
            text(insert),
            {"g": GUILD_ID, "seq": 1, "s": NOW + timedelta(days=14), "e": NOW + timedelta(days=28)},
        )


async def test_offer_cannot_go_live_without_approved_terms(
    sessions: async_sessionmaker[AsyncSession], guild: GuildSettings
) -> None:
    async with sessions() as s, s.begin():
        milestone = await s.scalar(
            text(
                "INSERT INTO milestone_definitions"
                " (guild_id, key, threshold_rp, title, kind, status)"
                " VALUES (:g, 'rp50', 50, '50 RP', 'offer', 'proposed') RETURNING id"
            ),
            {"g": GUILD_ID},
        )
    insert = (
        "INSERT INTO reward_offers (guild_id, milestone_id, title, status, terms, approved_by)"
        " VALUES (:g, :m, '15% off', 'live', :terms, :by)"
    )
    async with sessions() as s:
        with pytest.raises(IntegrityError, match="live_needs_approved_terms"):
            async with s.begin():
                await s.execute(
                    text(insert), {"g": GUILD_ID, "m": milestone, "terms": None, "by": 1}
                )
    async with sessions() as s, s.begin():
        await s.execute(
            text(insert), {"g": GUILD_ID, "m": milestone, "terms": "Approved terms", "by": 1}
        )


async def test_member_balances_cannot_go_negative(
    sessions: async_sessionmaker[AsyncSession], guild: GuildSettings
) -> None:
    async with sessions() as s, s.begin():
        await join(s, guild, discord_user_id=42, now=NOW)
    for statement in (
        "UPDATE members SET lifetime_rp = -1",
        "UPDATE members SET shield_balance = 11",
        "UPDATE members SET current_rank = 'emperor'",
    ):
        async with sessions() as s:
            with pytest.raises(IntegrityError):
                async with s.begin():
                    await s.execute(text(statement))
