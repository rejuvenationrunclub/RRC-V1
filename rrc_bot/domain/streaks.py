"""Per-member streak engine (plan §3.5): a pure fold over closed cycles.

This is the single source of truth for streak counts, Hot status, bonus targets and
shield use. The reconciler (Phase 3) reruns it whenever facts change and posts only
the difference to the ledgers, so the same facts always give the same answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Literal

from rrc_bot.domain.rules import ShieldRules, StreakRules

Outcome = Literal["qualified", "protected", "missed", "neutral", "not_enrolled"]


@dataclass(frozen=True)
class CycleWindow:
    cycle_id: int
    starts_at: datetime
    ends_at: datetime
    is_neutral: bool  # no qualifying event was held (Q-C1)


@dataclass(frozen=True)
class Activity:
    """A verified, streak-qualifying, in-person attendance."""

    attendance_id: int
    event_id: int
    event_starts_at: datetime
    group_key: str | None = None  # events sharing a key count once (Q-E1)


@dataclass(frozen=True)
class CycleResult:
    cycle_id: int
    outcome: Outcome
    count_before: int
    count_after: int
    hot_after: bool
    bonus_attendance_id: int | None = None
    shield_consumed: bool = False


def qualifying_in(cycle: CycleWindow, activities: Iterable[Activity]) -> list[Activity]:
    """Activities whose event starts in the cycle, one per group key, in (start, event id) order."""
    inside = sorted(
        (a for a in activities if cycle.starts_at <= a.event_starts_at < cycle.ends_at),
        key=lambda a: (a.event_starts_at, a.event_id, a.attendance_id),
    )
    seen: set[str] = set()
    result = []
    for activity in inside:
        if activity.group_key is not None:
            if activity.group_key in seen:
                continue
            seen.add(activity.group_key)
        result.append(activity)
    return result


def fold(
    *,
    enrolled_at: datetime,
    cycles: Sequence[CycleWindow],
    activities: Iterable[Activity],
    shield_credits: Iterable[tuple[datetime, int]],
    streak: StreakRules,
    shields: ShieldRules,
) -> list[CycleResult]:
    """Evaluate ``cycles`` (chronological, non-overlapping) for one member.

    ``shield_credits`` are every non-consumption shield change as ``(at, delta)``:
    enrollment and threshold grants, admin grants and revokes. Shields available at a
    cycle are the credits before its end minus what this fold already consumed.
    """
    ordered = sorted(cycles, key=lambda c: c.starts_at)
    for earlier, later in pairwise(ordered):
        if later.starts_at < earlier.ends_at:
            raise ValueError("cycles overlap")
    acts = list(activities)
    credits = sorted(shield_credits)

    count, hot, prev_protected, consumed = 0, False, False, 0
    results: list[CycleResult] = []
    for cycle in ordered:
        before = count
        if enrolled_at >= cycle.ends_at:
            results.append(CycleResult(cycle.cycle_id, "not_enrolled", before, count, hot))
            continue

        quals = qualifying_in(cycle, acts)
        if quals:
            count += 1
            hot = count >= streak.activation_cycles
            prev_protected = False
            bonus = quals[0].attendance_id if hot else None
            results.append(CycleResult(cycle.cycle_id, "qualified", before, count, hot, bonus))
            continue

        if cycle.is_neutral:
            results.append(CycleResult(cycle.cycle_id, "neutral", before, count, hot))
            continue

        available = sum(delta for at, delta in credits if at < cycle.ends_at) - consumed
        can_protect = (
            shields.auto_consume
            and count > 0  # Q-S1: nothing to protect at zero
            and available > 0
            and (shields.allow_consecutive or not prev_protected)
        )
        if can_protect:
            consumed += 1
            prev_protected = True
            results.append(
                CycleResult(cycle.cycle_id, "protected", before, count, hot, shield_consumed=True)
            )
            continue

        count, hot, prev_protected = 0, False, False
        results.append(CycleResult(cycle.cycle_id, "missed", before, count, hot))
    return results
