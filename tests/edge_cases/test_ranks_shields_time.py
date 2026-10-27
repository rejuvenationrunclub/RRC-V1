"""Plan §5 edge cases #8 (shield thresholds), #10-#12 (ranks) and cycle/time math."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from rrc_bot.domain import ranks, shields
from rrc_bot.domain.milestones import MilestoneSpec, newly_unlocked
from rrc_bot.domain.rules import default_rule_set
from rrc_bot.domain.timeutil import (
    PHOENIX,
    cycle_bounds,
    cycle_seq,
    format_local,
    local_datetime,
    local_midnight,
    week_key,
)

RULES = default_rule_set()
T0 = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)  # Mon Nov 2, 7:00 AM Phoenix


# --- shields #8 ---------------------------------------------------------------------


def test_thresholds_reached() -> None:
    assert shields.thresholds_reached(99, 100) == []
    assert shields.thresholds_reached(100, 100) == [100]
    assert shields.thresholds_reached(350, 100) == [100, 200, 300]


def test_reversal_below_threshold_and_back_never_regrants() -> None:
    decided = {100}  # granted at 100; then reversed to 90; now back at 110
    assert shields.undecided_thresholds(110, RULES.shields, decided) == []
    assert shields.undecided_thresholds(210, RULES.shields, decided) == [200]


def test_grant_at_cap_is_a_forfeit() -> None:
    assert shields.grant_outcome(0, RULES.shields) == "grant"
    assert shields.grant_outcome(1, RULES.shields) == "grant"
    assert shields.grant_outcome(2, RULES.shields) == "forfeit"


# --- ranks #10-#12 --------------------------------------------------------------------


def facts(
    rp: int, *, weeks: int, runs: list[datetime] | None = None, active: int = 0
) -> ranks.RankFacts:
    return ranks.RankFacts(
        enrolled_at=T0 - timedelta(weeks=weeks),
        lifetime_rp=rp,
        inperson_at=runs or [],
        active_weeks=active,
    )


def weekly_runs(n: int, *, every_days: int = 7) -> list[datetime]:
    return [T0 - timedelta(days=every_days * i) for i in range(n)]


def test_rp_before_time_requirement_waits_for_the_sweep() -> None:
    early = facts(150, weeks=3, runs=weekly_runs(4))
    assert ranks.promotion("initiate", early, RULES.ranks, T0) is None
    later = T0 + timedelta(weeks=9)
    assert ranks.promotion("initiate", early, RULES.ranks, later) == "relentless"


def test_relentless_needs_every_criterion() -> None:
    assert (
        ranks.eligible_rank(facts(150, weeks=12, runs=weekly_runs(3)), RULES.ranks, T0)
        == "initiate"
    )
    assert (
        ranks.eligible_rank(facts(149, weeks=12, runs=weekly_runs(4)), RULES.ranks, T0)
        == "initiate"
    )
    assert (
        ranks.eligible_rank(facts(150, weeks=12, runs=weekly_runs(4)), RULES.ranks, T0)
        == "relentless"
    )


def test_18_runs_must_fit_in_52_weeks() -> None:
    # 18 runs spaced 3 weeks apart span 51 weeks -> fits; 4 weeks apart span 68 -> doesn't.
    assert ranks.max_in_window(weekly_runs(18, every_days=21)) == 18
    assert ranks.max_in_window(weekly_runs(18, every_days=28)) < 18
    # boundary: first and last exactly 364 days apart are NOT in one 52-week window
    edge = [T0 + timedelta(days=364 * i / 17) for i in range(18)]
    assert ranks.max_in_window(edge) == 17
    assert ranks.max_in_window([]) == 0


def test_legacy_eligibility() -> None:
    runs = weekly_runs(18, every_days=14)  # 18 runs within 34 weeks
    ok = facts(700, weeks=52, runs=runs, active=40)
    assert ranks.eligible_rank(ok, RULES.ranks, T0) == "legacy"
    assert (
        ranks.eligible_rank(facts(700, weeks=52, runs=runs, active=39), RULES.ranks, T0)
        == "relentless"
    )


def test_ranks_never_demote() -> None:
    assert ranks.promotion("legacy", facts(0, weeks=0), RULES.ranks, T0) is None
    assert (
        ranks.promotion("relentless", facts(150, weeks=12, runs=weekly_runs(4)), RULES.ranks, T0)
        is None
    )


def test_active_weeks_count_distinct_local_weeks() -> None:
    sun_night_utc = datetime(2026, 11, 9, 5, 0, tzinfo=UTC)  # Sun Nov 8, 10 PM Phoenix
    mon = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)  # Mon Nov 9
    proof_days = [date(2026, 11, 10), date(2026, 11, 3)]  # Tue wk46, Tue wk45
    # Sunday night (local) belongs to the week before; same-week duplicates count once.
    assert ranks.active_week_count([sun_night_utc, mon], proof_days, PHOENIX) == 2
    assert ranks.active_week_count([], [date(2026, 11, 3)], PHOENIX) == 1


def test_next_rank_progress_for_profile() -> None:
    step = ranks.next_rank("initiate", facts(25, weeks=1, runs=weekly_runs(2)), RULES.ranks, T0)
    assert step is not None and step.rank == "relentless"
    assert [(c.label, c.have, c.need) for c in step.criteria] == [
        ("RP", 25, 150),
        ("weeks as a member", 1, 12),
        ("in-person runs", 2, 4),
    ]
    assert ranks.next_rank("legacy", facts(0, weeks=0), RULES.ranks, T0) is None


def test_milestones_unlock_once_in_order() -> None:
    specs = [MilestoneSpec("m100", 100), MilestoneSpec("m50", 50), MilestoneSpec("m300", 300)]
    assert [s.key for s in newly_unlocked(specs, 120, set())] == ["m50", "m100"]
    assert [s.key for s in newly_unlocked(specs, 120, {"m50"})] == ["m100"]
    assert newly_unlocked(specs, 49, set()) == []


# --- cycles and local time --------------------------------------------------------------

ANCHOR = date(2026, 11, 1)


def test_cycle_bounds_are_phoenix_midnights() -> None:
    start, end = cycle_bounds(0, ANCHOR, 14, PHOENIX)
    assert start == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)  # 00:00 MST = 07:00 UTC
    assert end - start == timedelta(days=14)
    assert cycle_bounds(1, ANCHOR, 14, PHOENIX)[0] == end
    assert cycle_bounds(-1, ANCHOR, 14, PHOENIX)[1] == start


@pytest.mark.parametrize(
    ("instant", "seq"),
    [
        (datetime(2026, 11, 1, 7, 0, tzinfo=UTC), 0),  # exactly cycle start
        (datetime(2026, 11, 15, 6, 59, tzinfo=UTC), 0),  # Nov 14, 11:59 PM Phoenix
        (datetime(2026, 11, 15, 7, 0, tzinfo=UTC), 1),
        (datetime(2026, 11, 1, 6, 59, tzinfo=UTC), -1),  # Oct 31 local: before the anchor
    ],
)
def test_cycle_seq(instant: datetime, seq: int) -> None:
    assert cycle_seq(instant, ANCHOR, 14, PHOENIX) == seq
    start, end = cycle_bounds(seq, ANCHOR, 14, PHOENIX)
    assert start <= instant < end


def test_local_time_helpers() -> None:
    assert local_midnight(date(2026, 11, 1), PHOENIX).hour == 7
    seven_am = local_datetime(date(2026, 11, 1), time(7, 0), PHOENIX)
    assert seven_am == datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
    assert format_local(seven_am, PHOENIX) == "Sun Nov 1, 7:00 AM"
    assert (
        format_local(seven_am + timedelta(hours=12, minutes=5), PHOENIX, with_date=False)
        == "7:05 PM"
    )
    assert week_key(date(2026, 11, 1)) == (2026, 44)  # Sunday ends ISO week 44
