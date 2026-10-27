"""Lifetime-RP milestones. Unlocking is recognition only; offers show only when live (L6)."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class MilestoneSpec:
    key: str
    threshold_rp: int


def newly_unlocked(
    specs: Iterable[MilestoneSpec], lifetime_rp: int, unlocked: Collection[str]
) -> list[MilestoneSpec]:
    """Milestones reached and not yet unlocked, lowest threshold first. Unlocks are permanent."""
    reached = [s for s in specs if s.threshold_rp <= lifetime_rp and s.key not in unlocked]
    return sorted(reached, key=lambda s: (s.threshold_rp, s.key))
