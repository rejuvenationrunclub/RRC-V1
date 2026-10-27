"""Plan §5 edge cases for the streak fold (#4-#9), plus property tests (§5 footnote)."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from rrc_bot.domain.rules import default_rule_set
from rrc_bot.domain.streaks import Activity, CycleResult, CycleWindow, fold
from rrc_bot.domain.timeutil import PHOENIX, cycle_bounds

RULES = default_rule_set()
ANCHOR = date(2026, 11, 1)


def cycle(seq: int, *, neutral: bool = False) -> CycleWindow:
    start, end = cycle_bounds(seq, ANCHOR, 14, PHOENIX)
    return CycleWindow(cycle_id=100 + seq, starts_at=start, ends_at=end, is_neutral=neutral)


def act(seq: int, day: int = 2, *, att: int | None = None, group: str | None = None) -> Activity:
    start = cycle(seq).starts_at + timedelta(days=day, hours=14)
    att_id = att if att is not None else 1000 + seq * 10 + day
    return Activity(attendance_id=att_id, event_id=att_id, event_starts_at=start, group_key=group)


ENROLLED = cycle(0).starts_at - timedelta(days=1)
ENROLLMENT_SHIELD = [(ENROLLED, 1)]


def run(
    pattern: str,
    *,
    credits: list[tuple[datetime, int]] | None = None,
    extra: list[Activity] | None = None,
    enrolled_at: datetime = ENROLLED,
) -> list[CycleResult]:
    """``pattern``: one char per cycle. Q = attended, M = missed, N = neutral (no events)."""
    cycles, acts = [], list(extra or [])
    for seq, ch in enumerate(pattern):
        cycles.append(cycle(seq, neutral=ch == "N"))
        if ch == "Q":
            acts.append(act(seq))
    return fold(
        enrolled_at=enrolled_at,
        cycles=cycles,
        activities=acts,
        shield_credits=ENROLLMENT_SHIELD if credits is None else credits,
        streak=RULES.streak,
        shields=RULES.shields,
    )


def outcomes(results: list[CycleResult]) -> str:
    return "".join(r.outcome[0].upper() for r in results)


def counts(results: list[CycleResult]) -> list[int]:
    return [r.count_after for r in results]


# --- #6 third-cycle activation -------------------------------------------------------


def test_bonus_starts_on_third_consecutive_cycle() -> None:
    r = run("QQQQ")
    assert counts(r) == [1, 2, 3, 4]
    assert [x.hot_after for x in r] == [False, False, True, True]
    assert [x.bonus_attendance_id is not None for x in r] == [False, False, True, True]


def test_protected_cycle_delays_activation_to_fourth() -> None:
    # Q, Q, S(protected), Q -> Hot (and bonus) only at the 4th cycle.
    r = run("QQMQ")
    assert outcomes(r) == "QQPQ"
    assert counts(r) == [1, 2, 2, 3]
    assert [x.bonus_attendance_id is not None for x in r] == [False, False, False, True]


# --- #5 two events in one cycle ------------------------------------------------------


def test_only_earliest_activity_gets_bonus_and_streak_counts_once() -> None:
    late = act(2, day=9, att=7002)
    early = act(2, day=1, att=7001)
    r = run("QQQ", extra=[late, early])
    assert counts(r) == [1, 2, 3]
    # cycle 2 has three activities (pattern's day-2 one, plus these); day 1 is earliest.
    assert r[2].bonus_attendance_id == 7001


def test_shared_group_key_counts_once() -> None:
    popup = act(0, day=3, att=1, group="popup-nov3")
    challenge = act(0, day=3, att=2, group="popup-nov3")
    r = fold(
        enrolled_at=ENROLLED,
        cycles=[cycle(0)],
        activities=[challenge, popup],
        shield_credits=[],
        streak=RULES.streak,
        shields=RULES.shields,
    )
    assert counts(r) == [1]


def test_bonus_tie_breaks_on_event_id() -> None:
    a = Activity(attendance_id=50, event_id=9, event_starts_at=cycle(2).starts_at)
    b = Activity(attendance_id=40, event_id=8, event_starts_at=cycle(2).starts_at)
    r = run("QQM", extra=[a, b])
    assert r[2].bonus_attendance_id == 40


# --- #4 / Q-C1 / Q-C2 neutral cycles ---------------------------------------------------


def test_neutral_cycle_between_qualified_keeps_count() -> None:
    r = run("QQNQ")
    assert outcomes(r) == "QQNQ"
    assert counts(r) == [1, 2, 2, 3]
    assert r[3].hot_after


def test_neutral_cycle_does_not_break_consecutive_protection_chain() -> None:
    # Q-C2: S, N, S counts as consecutive, so the second miss resets despite a shield.
    two_shields = [(ENROLLED, 1), (ENROLLED, 1)]
    r = run("QMNM", credits=two_shields)
    assert outcomes(r) == "QPNM"
    assert counts(r) == [1, 1, 1, 0]


def test_attendance_in_neutral_flagged_cycle_still_qualifies() -> None:
    # A member's own qualifying activity wins over the cycle-level neutral flag.
    r = fold(
        enrolled_at=ENROLLED,
        cycles=[cycle(0), cycle(1), cycle(2, neutral=True)],
        activities=[act(0), act(1), act(2)],
        shield_credits=[],
        streak=RULES.streak,
        shields=RULES.shields,
    )
    assert outcomes(r) == "QQQ"
    assert r[2].bonus_attendance_id is not None


# --- #8 shields: consecutive rule, count zero, availability ----------------------------


def test_two_consecutive_misses_reset_even_with_shields() -> None:
    r = run("QQMM", credits=[(ENROLLED, 1), (ENROLLED, 1)])
    assert outcomes(r) == "QQPM"
    assert counts(r)[-1] == 0
    assert sum(x.shield_consumed for x in r) == 1


def test_no_shield_used_when_count_is_zero() -> None:
    r = run("MQ")
    assert outcomes(r) == "MQ"
    assert not any(x.shield_consumed for x in r)


def test_no_shield_means_reset_and_hot_lost() -> None:
    r = run("QQQM", credits=[])
    assert counts(r) == [1, 2, 3, 0]
    assert r[-1].hot_after is False


def test_hot_survives_protection() -> None:
    # Q-S3: protected while Hot keeps the count and Hot; next qualified gets the bonus.
    r = run("QQQMQ")
    assert outcomes(r) == "QQQPQ"
    assert r[3].hot_after is True
    assert r[4].bonus_attendance_id is not None


def test_shield_earned_during_cycle_counts_for_that_cycle() -> None:
    late_grant = cycle(1).ends_at - timedelta(hours=1)
    r = run("QM", credits=[(late_grant, 1)])
    assert outcomes(r) == "QP"


def test_shield_granted_after_cycle_end_does_not_count() -> None:
    r = run("QM", credits=[(cycle(1).ends_at, 1)])
    assert outcomes(r) == "QM"


def test_one_shield_protects_once_then_runs_out() -> None:
    r = run("QMQMQ")
    assert outcomes(r) == "QPQMQ"
    assert counts(r) == [1, 1, 2, 0, 1]


def test_admin_revoke_reduces_available_shields() -> None:
    r = run("QM", credits=[(ENROLLED, 1), (ENROLLED + timedelta(days=1), -1)])
    assert outcomes(r) == "QM"


def test_consecutive_protection_when_rules_allow_it() -> None:
    lenient = RULES.shields.model_copy(update={"allow_consecutive": True})
    cycles = [cycle(0), cycle(1), cycle(2)]
    r = fold(
        enrolled_at=ENROLLED,
        cycles=cycles,
        activities=[act(0)],
        shield_credits=[(ENROLLED, 1), (ENROLLED, 1)],
        streak=RULES.streak,
        shields=lenient,
    )
    assert outcomes(r) == "QPP"


def test_auto_consume_off_never_protects() -> None:
    manual = RULES.shields.model_copy(update={"auto_consume": False})
    r = fold(
        enrolled_at=ENROLLED,
        cycles=[cycle(0), cycle(1)],
        activities=[act(0)],
        shield_credits=ENROLLMENT_SHIELD,
        streak=RULES.streak,
        shields=manual,
    )
    assert outcomes(r) == "QM"


# --- enrollment ----------------------------------------------------------------------


def test_cycles_before_enrollment_are_not_enrolled() -> None:
    joined = cycle(1).starts_at + timedelta(days=3)
    r = run("MQQ", enrolled_at=joined)
    assert outcomes(r) == "NQQ"
    assert r[0].outcome == "not_enrolled"
    assert counts(r) == [0, 1, 2]


def test_rejects_overlapping_cycles() -> None:
    bad = CycleWindow(999, cycle(0).starts_at + timedelta(days=1), cycle(1).ends_at, False)
    try:
        fold(
            enrolled_at=ENROLLED,
            cycles=[cycle(0), bad],
            activities=[],
            shield_credits=[],
            streak=RULES.streak,
            shields=RULES.shields,
        )
    except ValueError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("overlapping cycles accepted")


# --- properties ------------------------------------------------------------------------

patterns = st.lists(st.sampled_from("QMN"), min_size=1, max_size=12).map("".join)


@st.composite
def histories(draw: st.DrawFn) -> tuple[str, list[Activity], list[tuple[datetime, int]]]:
    pattern = draw(patterns)
    acts: list[Activity] = []
    for seq, ch in enumerate(pattern):
        if ch == "Q":
            for n in range(draw(st.integers(1, 3))):
                day = draw(st.integers(0, 13))
                group = draw(st.sampled_from([None, f"g{seq}"]))
                acts.append(act(seq, day, att=seq * 100 + n * 20 + day, group=group))
    n_credits = draw(st.integers(0, 4))
    credits = [
        (ENROLLED + timedelta(days=draw(st.integers(0, 14 * len(pattern)))), 1)
        for _ in range(n_credits)
    ]
    return pattern, acts, credits


def _fold(
    pattern: str, acts: list[Activity], credits: list[tuple[datetime, int]]
) -> list[CycleResult]:
    return fold(
        enrolled_at=ENROLLED,
        cycles=[cycle(seq, neutral=ch == "N") for seq, ch in enumerate(pattern)],
        activities=acts,
        shield_credits=credits,
        streak=RULES.streak,
        shields=RULES.shields,
    )


@settings(max_examples=300, deadline=None)
@given(histories(), st.randoms(use_true_random=False))
def test_fold_is_deterministic_and_order_independent(
    history: tuple[str, list[Activity], list[tuple[datetime, int]]], rnd: random.Random
) -> None:
    pattern, acts, credits = history
    expected = _fold(pattern, acts, credits)
    shuffled_acts, shuffled_credits = acts[:], credits[:]
    rnd.shuffle(shuffled_acts)
    rnd.shuffle(shuffled_credits)
    assert _fold(pattern, shuffled_acts, shuffled_credits) == expected


@settings(max_examples=300, deadline=None)
@given(histories())
def test_fold_invariants(history: tuple[str, list[Activity], list[tuple[datetime, int]]]) -> None:
    pattern, acts, credits = history
    results = _fold(pattern, acts, credits)
    assert len(results) == len(pattern)
    consumed = 0
    prev_protected = False
    for res, ch in zip(results, pattern, strict=True):
        assert res.count_before >= 0 and res.count_after >= 0
        # at most one bonus per member-cycle, only when Hot, only on a qualified cycle
        if res.bonus_attendance_id is not None:
            assert res.outcome == "qualified" and res.hot_after
        assert res.hot_after == (res.count_after >= RULES.streak.activation_cycles)
        if res.outcome == "qualified":
            assert ch == "Q" and res.count_after == res.count_before + 1
        elif res.outcome in ("neutral", "protected"):
            assert res.count_after == res.count_before
        elif res.outcome == "missed":
            assert res.count_after == 0
        if res.shield_consumed:
            consumed += 1
            assert not prev_protected, "two consecutive protections"
        if res.outcome != "neutral":
            prev_protected = res.outcome == "protected"
        # shields used never exceed shields credited
        assert consumed <= len(credits)
