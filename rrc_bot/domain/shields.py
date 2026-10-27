"""Shield grants (plan §2.3, Q-S4, Q-S5, §5 #8).

A shield is granted at every ``grant_every_rp`` of lifetime RP (all RP counts, Q-S5).
Each threshold is decided exactly once: granted, or recorded as forfeited when the
member is at the cap. Dropping below a threshold and climbing back never re-earns it.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from rrc_bot.domain.rules import ShieldRules

GrantOutcome = Literal["grant", "forfeit"]


def thresholds_reached(lifetime_rp: int, every: int) -> list[int]:
    """Every threshold at or below ``lifetime_rp``: 100, 200, ... (ascending)."""
    if every <= 0:
        raise ValueError("grant interval must be positive")
    return list(range(every, max(lifetime_rp, 0) + 1, every))


def undecided_thresholds(
    lifetime_rp: int, rules: ShieldRules, decided: Collection[int]
) -> list[int]:
    """Thresholds reached but not yet granted or forfeited, ascending."""
    return [t for t in thresholds_reached(lifetime_rp, rules.grant_every_rp) if t not in decided]


def grant_outcome(balance: int, rules: ShieldRules) -> GrantOutcome:
    return "grant" if balance < rules.max_balance else "forfeit"
