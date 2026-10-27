"""Baseline: extensions and the append-only guard used by ledger tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Needed for the cycles no-overlap EXCLUDE constraint (Phase 1).
    # btree_gist is a trusted extension (PG13+), so a database owner can create it.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # Attached as BEFORE UPDATE OR DELETE trigger on rp_ledger, shield_ledger,
    # admin_audit_log, member_unlocks and rank_promotions. Corrections are made
    # with compensating rows, never by editing history.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rrc_forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table "%" is append-only; % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS rrc_forbid_mutation()")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
