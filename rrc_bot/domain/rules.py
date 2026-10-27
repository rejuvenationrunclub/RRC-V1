"""Versioned scoring rules (plan §2.3).

A ``RuleSet`` is stored as JSON in ``rule_sets.config``. Every RP ledger row
records the rule-set version it was computed under, so changing a value later
never rewrites history.
"""

from __future__ import annotations

from decimal import Decimal
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Rounding = Literal["half_up", "floor", "ceil"]


class _Rules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RPRules(_Rules):
    flagship: int = Field(gt=0)
    popup_allowed: tuple[int, ...] = Field(min_length=1)
    proof_per_day: int = Field(gt=0)
    proof_week_cap: int = Field(gt=0)

    @field_validator("popup_allowed")
    @classmethod
    def _positive_sorted_unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(v <= 0 for v in value):
            raise ValueError("pop-up RP values must be positive")
        return tuple(sorted(set(value)))


class StreakRules(_Rules):
    activation_cycles: int = Field(ge=1)
    multiplier: Decimal = Field(ge=1)
    rounding: Rounding = "half_up"
    bonus_applies_to: Literal["first_qualifying_activity_per_cycle"] = (
        "first_qualifying_activity_per_cycle"
    )
    late_checkin_grace_hours: int = Field(ge=0)

    @field_validator("multiplier", mode="before")
    @classmethod
    def _exact_decimal(cls, value: object) -> object:
        # YAML/JSON floats are binary; go through str so 1.1 means exactly 1.1.
        return str(value) if isinstance(value, float) else value


class ShieldRules(_Rules):
    enrollment_grant: int = Field(ge=0)
    grant_every_rp: int = Field(gt=0)
    max_balance: int = Field(ge=0, le=10)  # members.shield_balance CHECK allows 0..10
    auto_consume: bool = True
    allow_consecutive: bool = False

    @model_validator(mode="after")
    def _grant_fits_cap(self) -> ShieldRules:
        if self.enrollment_grant > self.max_balance:
            raise ValueError("enrollment_grant cannot exceed max_balance")
        return self


class RelentlessRules(_Rules):
    min_rp: int = Field(ge=0)
    min_weeks_enrolled: int = Field(ge=0)
    min_inperson: int = Field(ge=0)


class LegacyRules(_Rules):
    min_rp: int = Field(ge=0)
    min_weeks_enrolled: int = Field(ge=0)
    min_inperson_in_52w: int = Field(ge=0)
    min_active_weeks: int = Field(ge=0)


class RankRules(_Rules):
    relentless: RelentlessRules
    legacy: LegacyRules


class RuleSet(_Rules):
    label: str = Field(min_length=1)
    cycle_length_days: int = Field(gt=0)
    rp: RPRules
    streak: StreakRules
    shields: ShieldRules
    ranks: RankRules

    def to_json(self) -> dict[str, object]:
        """JSON-safe form for ``rule_sets.config`` (Decimal becomes a string)."""
        return self.model_dump(mode="json")


def load_rule_set(text: str) -> RuleSet:
    """Parse and validate a YAML (or JSON) rule set. Raises ``ValueError`` on bad input."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("rule set must be a mapping")
    return RuleSet.model_validate(data)


def default_rule_set() -> RuleSet:
    """The seeded rules, every value PROPOSED (plan §8-B)."""
    text = resources.files("rrc_bot.domain").joinpath("rules_default.yaml").read_text("utf-8")
    return load_rule_set(text)
