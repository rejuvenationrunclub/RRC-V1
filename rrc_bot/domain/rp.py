"""RP amounts: event base values and the Hot Streak multiplier (plan §2.3, §5 #7)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from rrc_bot.domain.rules import Rounding, RuleSet, StreakRules


def apply_multiplier(base: int, multiplier: Decimal, rounding: Rounding) -> int:
    """``base * multiplier`` rounded to an integer with exact (rational) arithmetic.

    half_up with 1.5 is ``(base * 3 + 1) // 2``: 3 -> 5, 5 -> 8, 10 -> 15, 0 -> 0.
    """
    if base < 0:
        raise ValueError("base RP cannot be negative")
    exact = Fraction(base) * Fraction(multiplier)
    if rounding == "half_up":
        return math.floor(exact + Fraction(1, 2))
    if rounding == "floor":
        return math.floor(exact)
    return math.ceil(exact)


@dataclass(frozen=True)
class BonusCalc:
    base: int
    multiplier: Decimal
    rounding: Rounding
    total: int

    @property
    def extra(self) -> int:
        """What the streak_bonus ledger entry adds on top of the base entry."""
        return self.total - self.base

    def as_json(self) -> dict[str, object]:
        return {
            "base": self.base,
            "multiplier": str(self.multiplier),
            "rounding": self.rounding,
            "total": self.total,
        }


def streak_bonus(base: int, rules: StreakRules) -> BonusCalc:
    total = apply_multiplier(base, rules.multiplier, rules.rounding)
    return BonusCalc(base=base, multiplier=rules.multiplier, rounding=rules.rounding, total=total)


@dataclass(frozen=True)
class EventTypeSpec:
    """Seed values for an ``event_types`` row, derived from a rule set."""

    key: str
    name: str
    default_base_rp: int
    allowed_base_rp: tuple[int, ...]
    streak_qualifying: bool
    in_person: bool


def event_type_specs(rules: RuleSet) -> tuple[EventTypeSpec, ...]:
    """Flagship runs and pop-up runs (§2). Other activity types get added with their own rules."""
    return (
        EventTypeSpec(
            key="flagship",
            name="Flagship run",
            default_base_rp=rules.rp.flagship,
            allowed_base_rp=(rules.rp.flagship,),
            streak_qualifying=True,
            in_person=True,
        ),
        EventTypeSpec(
            key="popup_run",
            name="Pop-up run",
            default_base_rp=rules.rp.popup_allowed[0],
            allowed_base_rp=rules.rp.popup_allowed,
            streak_qualifying=True,
            in_person=True,
        ),
    )


def resolve_base_rp(
    requested: int | None, *, default_base_rp: int, allowed_base_rp: tuple[int, ...]
) -> int:
    """The base RP an event snapshots at creation. Raises ``ValueError`` if not allowed."""
    value = default_base_rp if requested is None else requested
    if allowed_base_rp and value not in allowed_base_rp:
        allowed = ", ".join(str(v) for v in allowed_base_rp)
        raise ValueError(f"base RP must be one of: {allowed}")
    if value < 0:
        raise ValueError("base RP cannot be negative")
    return value
