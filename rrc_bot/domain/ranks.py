"""Permanent ranks: Initiate -> Relentless -> Legacy (plan §2.3, Q-K1..Q-K4, §5 #10-12).

Eligibility needs every criterion at once. Ranks never go down (Q-K4); a member whose
RP arrives before the time requirement is promoted later by the daily sweep.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from rrc_bot.domain.rules import RankRules
from rrc_bot.domain.timeutil import local_date, week_key

RANKS = ("initiate", "relentless", "legacy")
RANK_TITLES = {"initiate": "Initiate", "relentless": "Relentless", "legacy": "Legacy"}
WINDOW_52W = timedelta(days=364)


@dataclass(frozen=True)
class RankFacts:
    enrolled_at: datetime
    lifetime_rp: int
    inperson_at: Sequence[datetime]  # verified in-person attendances (event start times)
    active_weeks: int  # distinct local ISO weeks with in-person or approved proof (Q-K2)


@dataclass(frozen=True)
class Criterion:
    label: str
    have: int
    need: int

    @property
    def met(self) -> bool:
        return self.have >= self.need


@dataclass(frozen=True)
class RankProgress:
    rank: str
    criteria: tuple[Criterion, ...]

    @property
    def eligible(self) -> bool:
        return all(c.met for c in self.criteria)


def weeks_enrolled(enrolled_at: datetime, now: datetime) -> int:
    return max((now - enrolled_at).days // 7, 0)


def max_in_window(times: Iterable[datetime], window: timedelta = WINDOW_52W) -> int:
    """The most timestamps inside any half-open window ``[t, t + window)`` (Q-K1: any window)."""
    ordered = sorted(times)
    best, start = 0, 0
    for end, t in enumerate(ordered):
        while t - ordered[start] >= window:
            start += 1
        best = max(best, end - start + 1)
    return best


def active_week_count(
    inperson_at: Iterable[datetime], proof_days: Iterable[date], tz: ZoneInfo
) -> int:
    weeks = {week_key(local_date(t, tz)) for t in inperson_at}
    weeks.update(week_key(d) for d in proof_days)
    return len(weeks)


def progress(facts: RankFacts, rules: RankRules, now: datetime) -> tuple[RankProgress, ...]:
    """Progress toward each rank above Initiate, lowest first."""
    weeks = weeks_enrolled(facts.enrolled_at, now)
    rel, leg = rules.relentless, rules.legacy
    return (
        RankProgress(
            "relentless",
            (
                Criterion("RP", facts.lifetime_rp, rel.min_rp),
                Criterion("weeks as a member", weeks, rel.min_weeks_enrolled),
                Criterion("in-person runs", len(facts.inperson_at), rel.min_inperson),
            ),
        ),
        RankProgress(
            "legacy",
            (
                Criterion("RP", facts.lifetime_rp, leg.min_rp),
                Criterion("weeks as a member", weeks, leg.min_weeks_enrolled),
                Criterion(
                    "in-person runs within 52 weeks",
                    max_in_window(facts.inperson_at),
                    leg.min_inperson_in_52w,
                ),
                Criterion("active weeks", facts.active_weeks, leg.min_active_weeks),
            ),
        ),
    )


def eligible_rank(facts: RankFacts, rules: RankRules, now: datetime) -> str:
    rank = "initiate"
    for step in progress(facts, rules, now):
        if step.eligible:
            rank = step.rank
    return rank


def promotion(current: str, facts: RankFacts, rules: RankRules, now: datetime) -> str | None:
    """The rank to promote to, or None. Never demotes (Q-K4)."""
    target = eligible_rank(facts, rules, now)
    return target if RANKS.index(target) > RANKS.index(current) else None


def next_rank(
    current: str, facts: RankFacts, rules: RankRules, now: datetime
) -> RankProgress | None:
    """Progress toward the next rank above ``current``, or None at the top."""
    above = RANKS[RANKS.index(current) + 1 :]
    for step in progress(facts, rules, now):
        if step.rank in above:
            return step
    return None
