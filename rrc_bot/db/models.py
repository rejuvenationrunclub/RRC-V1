"""ORM models for the full schema (plan §2.2).

Every timestamp is ``timestamptz`` in UTC and every Discord snowflake is ``bigint``.
Ledger-like tables are append-only, enforced by triggers installed in the migration
(``APPEND_ONLY_TABLES``). Unique constraints are the last line of defence for
idempotency: services rely on them rather than on read-then-write checks.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from rrc_bot.db.base import Base

TZ = DateTime(timezone=True)

APPEND_ONLY_TABLES = (
    "rp_ledger",
    "shield_ledger",
    "admin_audit_log",
    "member_unlocks",
    "rank_promotions",
)

RANKS = ("initiate", "relentless", "legacy")


def one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _now() -> Any:
    return func.now()


def _empty_json() -> Any:
    return text("'{}'::jsonb")


# --- Configuration ---------------------------------------------------------------------


class GuildConfig(Base):
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    display_tz: Mapped[str] = mapped_column(Text, server_default="America/Phoenix")
    cycle_anchor_utc: Mapped[datetime] = mapped_column(TZ)
    channel_ids: Mapped[dict[str, int]] = mapped_column(JSONB, server_default=_empty_json())
    role_ids: Mapped[dict[str, int]] = mapped_column(JSONB, server_default=_empty_json())
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


RULE_SET_STATUSES = ("draft", "active", "retired")


class RuleSetRow(Base):
    """Versioned rules. Exactly one ``active`` per guild; non-draft rows are frozen."""

    __tablename__ = "rule_sets"
    __table_args__ = (
        UniqueConstraint("guild_id", "version"),
        CheckConstraint(one_of("status", RULE_SET_STATUSES), name="status"),
        Index(
            "uq_rule_sets_one_active",
            "guild_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    label: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[datetime | None] = mapped_column(TZ)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


class EventType(Base):
    __tablename__ = "event_types"
    __table_args__ = (
        UniqueConstraint("guild_id", "key"),
        CheckConstraint("default_base_rp >= 0", name="default_base_rp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    default_base_rp: Mapped[int] = mapped_column(Integer)
    allowed_base_rp: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'::integer[]")
    )
    streak_qualifying: Mapped[bool] = mapped_column(Boolean)
    in_person: Mapped[bool] = mapped_column(Boolean)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


# --- Members -----------------------------------------------------------------------------

MEMBER_STATUSES = ("active", "left", "anonymized")


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("guild_id", "discord_user_id"),
        CheckConstraint(one_of("status", MEMBER_STATUSES), name="status"),
        CheckConstraint("lifetime_rp >= 0", name="lifetime_rp"),
        CheckConstraint(one_of("current_rank", RANKS), name="current_rank"),
        CheckConstraint("shield_balance BETWEEN 0 AND 10", name="shield_balance"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    # NULL only once anonymized (Q-M3).
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger)
    enrolled_at: Mapped[datetime] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    lifetime_rp: Mapped[int] = mapped_column(Integer, server_default="0")  # cache of SUM(ledger)
    current_rank: Mapped[str] = mapped_column(Text, server_default="initiate")
    shield_balance: Mapped[int] = mapped_column(Integer, server_default="0")  # cache
    consents: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_empty_json())
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


class MembershipPeriod(Base):
    __tablename__ = "membership_periods"
    __table_args__ = (CheckConstraint("left_at IS NULL OR left_at >= joined_at", name="order"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(TZ)
    left_at: Mapped[datetime | None] = mapped_column(TZ)


# --- Cycles & events ---------------------------------------------------------------------

CYCLE_STATUSES = ("upcoming", "open", "grace", "closing", "closed")


class Cycle(Base):
    __tablename__ = "cycles"
    __table_args__ = (
        UniqueConstraint("guild_id", "seq"),
        CheckConstraint(one_of("status", CYCLE_STATUSES), name="status"),
        CheckConstraint("ends_at > starts_at", name="order"),
        ExcludeConstraint(
            ("guild_id", "="),
            (func.tstzrange(text("starts_at"), text("ends_at")), "&&"),
            using="gist",
            name="ex_cycles_no_overlap",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    seq: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(TZ)
    ends_at: Mapped[datetime] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(Text)
    is_neutral: Mapped[bool | None] = mapped_column(Boolean)  # set at close (Q-C1)
    closed_at: Mapped[datetime | None] = mapped_column(TZ)
    closed_by: Mapped[int | None] = mapped_column(BigInteger)  # NULL = scheduler


EVENT_STATUSES = ("draft", "scheduled", "checkin_open", "checkin_closed", "finalized", "canceled")
VERIFICATION_MODES = ("code", "code_plus_roster", "staff_only")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(one_of("status", EVENT_STATUSES), name="status"),
        CheckConstraint(one_of("verification_mode", VERIFICATION_MODES), name="verification_mode"),
        CheckConstraint("ends_at > starts_at", name="order"),
        CheckConstraint("checkin_closes_at > checkin_opens_at", name="checkin_window"),
        CheckConstraint("base_rp >= 0", name="base_rp"),
        CheckConstraint("code_period_s > 0", name="code_period_s"),
        CheckConstraint("status <> 'canceled' OR cancel_reason IS NOT NULL", name="cancel_reason"),
        Index("ix_events_guild_starts", "guild_id", "starts_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    event_type_id: Mapped[int] = mapped_column(ForeignKey("event_types.id"))
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(TZ)
    ends_at: Mapped[datetime] = mapped_column(TZ)
    # Snapshots taken at creation; never recomputed when rules change (§5 #15).
    base_rp: Mapped[int] = mapped_column(Integer)
    streak_qualifying: Mapped[bool] = mapped_column(Boolean)
    in_person: Mapped[bool] = mapped_column(Boolean)
    activity_group_key: Mapped[str | None] = mapped_column(Text)
    checkin_opens_at: Mapped[datetime] = mapped_column(TZ)
    checkin_closes_at: Mapped[datetime] = mapped_column(TZ)
    verification_mode: Mapped[str] = mapped_column(Text, server_default="code_plus_roster")
    code_secret: Mapped[bytes] = mapped_column(LargeBinary)
    code_period_s: Mapped[int] = mapped_column(Integer, server_default="60")
    status: Mapped[str] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


ATTENDANCE_STATUSES = ("pending", "verified", "rejected", "reversed")
ATTENDANCE_METHODS = ("code", "staff_roster", "staff_manual", "late_exception")


class Attendance(Base):
    __tablename__ = "attendances"
    __table_args__ = (
        UniqueConstraint("event_id", "member_id"),  # idempotent check-in
        CheckConstraint(one_of("status", ATTENDANCE_STATUSES), name="status"),
        CheckConstraint(one_of("method", ATTENDANCE_METHODS), name="method"),
        CheckConstraint("status <> 'verified' OR verified_at IS NOT NULL", name="verified_at"),
        # Events sharing an activity group count once per member (Q-E1).
        Index(
            "uq_attendances_member_activity_group",
            "member_id",
            "activity_group_key",
            unique=True,
            postgresql_where=text(
                "activity_group_key IS NOT NULL AND status IN ('pending', 'verified')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    status: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    checked_in_at: Mapped[datetime] = mapped_column(TZ)
    verified_at: Mapped[datetime | None] = mapped_column(TZ)
    verified_by: Mapped[int | None] = mapped_column(BigInteger)
    is_late: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reason: Mapped[str | None] = mapped_column(Text)
    source_interaction_id: Mapped[int | None] = mapped_column(BigInteger)
    activity_group_key: Mapped[str | None] = mapped_column(Text)  # denormalized from the event


class CheckinAttempt(Base):
    __tablename__ = "checkin_attempts"
    __table_args__ = (Index("ix_checkin_attempts_member_time", "member_id", "attempted_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    attempted_at: Mapped[datetime] = mapped_column(TZ)
    success: Mapped[bool] = mapped_column(Boolean)


# --- Proof of progress -------------------------------------------------------------------

PROOF_STATUSES = ("pending", "approved", "rejected", "reversed", "withdrawn")


class ProofSubmission(Base):
    __tablename__ = "proof_submissions"
    __table_args__ = (
        CheckConstraint(one_of("status", PROOF_STATUSES), name="status"),
        CheckConstraint("length(note) >= 20", name="note_length"),
        Index(
            "uq_proof_submissions_one_approved_per_day",
            "member_id",
            "activity_date",
            unique=True,
            postgresql_where=text("status = 'approved'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    activity_date: Mapped[date] = mapped_column(Date)  # local calendar date
    note: Mapped[str] = mapped_column(Text)
    review_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(TZ)
    review_reason: Mapped[str | None] = mapped_column(Text)
    source_interaction_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    submitted_at: Mapped[datetime] = mapped_column(TZ)


# --- Ledgers (append-only) ---------------------------------------------------------------

RP_ENTRY_TYPES = (
    "attendance_base",
    "streak_bonus",
    "streak_bonus_correction",
    "proof",
    "manual_adjustment",
    "reversal",
)


class RPLedger(Base):
    __tablename__ = "rp_ledger"
    __table_args__ = (
        CheckConstraint(one_of("entry_type", RP_ENTRY_TYPES), name="entry_type"),
        CheckConstraint("amount <> 0", name="amount"),
        CheckConstraint("balance_after >= 0", name="balance_after"),
        Index("ix_rp_ledger_member", "member_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    entry_type: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    cycle_id: Mapped[int | None] = mapped_column(ForeignKey("cycles.id"))
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"))
    calc: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reverses_entry_id: Mapped[int | None] = mapped_column(ForeignKey("rp_ledger.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL = system
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


SHIELD_KINDS = (
    "enrollment_grant",
    "rp_threshold_grant",
    "rp_threshold_forfeit_at_cap",
    "consumed",
    "consumption_refund",
    "admin_grant",
    "admin_revoke",
)


class ShieldLedger(Base):
    __tablename__ = "shield_ledger"
    __table_args__ = (
        CheckConstraint(one_of("kind", SHIELD_KINDS), name="kind"),
        CheckConstraint("delta IN (-1, 0, 1)", name="delta"),
        CheckConstraint("balance_after >= 0", name="balance_after"),
        Index(
            "uq_shield_ledger_once_per_threshold",
            "member_id",
            "rp_threshold",
            unique=True,
            postgresql_where=text("kind IN ('rp_threshold_grant', 'rp_threshold_forfeit_at_cap')"),
        ),
        Index("ix_shield_ledger_member", "member_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    kind: Mapped[str] = mapped_column(Text)
    delta: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    rp_threshold: Mapped[int | None] = mapped_column(Integer)
    cycle_id: Mapped[int | None] = mapped_column(ForeignKey("cycles.id"))
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


# --- Streak projections (recomputable) -----------------------------------------------------

CYCLE_OUTCOMES = ("qualified", "protected", "missed", "neutral", "not_enrolled")


class MemberCycleResult(Base):
    __tablename__ = "member_cycle_results"
    __table_args__ = (CheckConstraint(one_of("outcome", CYCLE_OUTCOMES), name="outcome"),)

    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id"), primary_key=True)
    outcome: Mapped[str] = mapped_column(Text)
    count_before: Mapped[int] = mapped_column(Integer)
    count_after: Mapped[int] = mapped_column(Integer)
    hot_after: Mapped[bool] = mapped_column(Boolean)
    bonus_attendance_id: Mapped[int | None] = mapped_column(ForeignKey("attendances.id"))
    computed_at: Mapped[datetime] = mapped_column(TZ)
    engine_version: Mapped[int] = mapped_column(Integer)


class MemberStreakState(Base):
    __tablename__ = "member_streak_state"

    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, server_default="0")
    hot: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    last_closed_cycle_id: Mapped[int | None] = mapped_column(ForeignKey("cycles.id"))
    last_cycle_protected: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


# --- Recognition ---------------------------------------------------------------------------

MILESTONE_KINDS = ("recognition", "rank_gate", "offer")
MILESTONE_STATUSES = ("launch", "proposed", "partner_pending", "future", "retired")


class MilestoneDefinition(Base):
    __tablename__ = "milestone_definitions"
    __table_args__ = (
        UniqueConstraint("guild_id", "key"),
        CheckConstraint(one_of("kind", MILESTONE_KINDS), name="kind"),
        CheckConstraint(one_of("status", MILESTONE_STATUSES), name="status"),
        CheckConstraint("threshold_rp >= 0", name="threshold_rp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    key: Mapped[str] = mapped_column(Text)
    threshold_rp: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    member_visible: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class MemberUnlock(Base):
    __tablename__ = "member_unlocks"
    __table_args__ = (UniqueConstraint("member_id", "milestone_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestone_definitions.id"))
    unlocked_at: Mapped[datetime] = mapped_column(TZ)
    rp_at_unlock: Mapped[int] = mapped_column(Integer)
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"))


class RankPromotion(Base):
    __tablename__ = "rank_promotions"
    __table_args__ = (
        UniqueConstraint("member_id", "to_rank"),
        CheckConstraint(one_of("from_rank", RANKS), name="from_rank"),
        CheckConstraint(one_of("to_rank", RANKS), name="to_rank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    from_rank: Mapped[str] = mapped_column(Text)
    to_rank: Mapped[str] = mapped_column(Text)
    promoted_at: Mapped[datetime] = mapped_column(TZ)
    criteria_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)


OFFER_STATUSES = ("draft", "approved", "live", "paused", "expired")


class RewardOffer(Base):
    """Members only ever see rows that are live, approved and inside their date window."""

    __tablename__ = "reward_offers"
    __table_args__ = (
        CheckConstraint(one_of("status", OFFER_STATUSES), name="status"),
        # Never live without approved terms (plan §0, L5).
        CheckConstraint(
            "status <> 'live' OR (terms IS NOT NULL AND approved_by IS NOT NULL)",
            name="live_needs_approved_terms",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestone_definitions.id"))
    partner_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    terms: Mapped[str | None] = mapped_column(Text)
    redemption_instructions: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="draft")
    starts_at: Mapped[datetime | None] = mapped_column(TZ)
    ends_at: Mapped[datetime | None] = mapped_column(TZ)
    per_member_limit: Mapped[int | None] = mapped_column(Integer)
    total_limit: Mapped[int | None] = mapped_column(Integer)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(TZ)


NOMINATION_STATUSES = (
    "submitted",
    "awaiting_consent",
    "consented",
    "declined",
    "approved",
    "published",
    "rejected",
)


class Nomination(Base):
    __tablename__ = "nominations"
    __table_args__ = (CheckConstraint(one_of("status", NOMINATION_STATUSES), name="status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    nominator_member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    nominee_member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    category: Mapped[str] = mapped_column(Text)
    story: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    consent_name: Mapped[bool | None] = mapped_column(Boolean)
    consent_photo: Mapped[bool | None] = mapped_column(Boolean)
    consent_at: Mapped[datetime | None] = mapped_column(TZ)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    published_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


# --- Ops -------------------------------------------------------------------------------------


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"
    __table_args__ = (Index("ix_admin_audit_log_guild_time", "guild_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(ForeignKey("guild_config.guild_id"))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_role_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=text("'{}'::bigint[]")
    )
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    interaction_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


class ProcessedInteraction(Base):
    __tablename__ = "processed_interactions"

    interaction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    command: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())


OUTBOX_KINDS = ("dm", "channel_post", "role_add", "role_remove")
OUTBOX_STATUSES = ("pending", "sending", "sent", "failed")


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(one_of("kind", OUTBOX_KINDS), name="kind"),
        CheckConstraint(one_of("status", OUTBOX_STATUSES), name="status"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=_now())
    sent_at: Mapped[datetime | None] = mapped_column(TZ)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (UniqueConstraint("job_name", "scope_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_name: Mapped[str] = mapped_column(Text)
    scope_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
