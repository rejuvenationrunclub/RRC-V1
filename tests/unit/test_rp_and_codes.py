from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from rrc_bot.domain import codes
from rrc_bot.domain.rp import (
    apply_multiplier,
    event_type_specs,
    resolve_base_rp,
    streak_bonus,
)
from rrc_bot.domain.rules import default_rule_set

RULES = default_rule_set()


# §5 #7: half_up on integer arithmetic, (base*3 + 1) // 2 for 1.5.
@pytest.mark.parametrize(("base", "total"), [(3, 5), (5, 8), (10, 15), (0, 0), (1, 2), (7, 11)])
def test_half_up_rounding(base: int, total: int) -> None:
    assert apply_multiplier(base, Decimal("1.5"), "half_up") == total
    assert apply_multiplier(base, Decimal("1.5"), "half_up") == (base * 3 + 1) // 2


@pytest.mark.parametrize(("rounding", "expected"), [("half_up", 5), ("floor", 4), ("ceil", 5)])
def test_rounding_modes_are_configurable(rounding: str, expected: int) -> None:
    assert apply_multiplier(3, Decimal("1.5"), rounding) == expected  # type: ignore[arg-type]


def test_multiplier_arithmetic_is_exact() -> None:
    # 10 * 1.15 = 11.5 exactly -> 12 (binary floats would give 11.499999...)
    assert apply_multiplier(10, Decimal("1.15"), "half_up") == 12


def test_negative_base_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        apply_multiplier(-1, Decimal("1.5"), "half_up")


def test_streak_bonus_extra_and_calc_json() -> None:
    calc = streak_bonus(5, RULES.streak)
    assert (calc.base, calc.total, calc.extra) == (5, 8, 3)
    assert calc.as_json() == {"base": 5, "multiplier": "1.5", "rounding": "half_up", "total": 8}


def test_event_types_from_rules() -> None:
    specs = {s.key: s for s in event_type_specs(RULES)}
    assert set(specs) == {"flagship", "popup_run"}
    assert specs["flagship"].allowed_base_rp == (10,)
    assert specs["popup_run"].allowed_base_rp == (3, 5)
    assert all(s.streak_qualifying and s.in_person for s in specs.values())


def test_resolve_base_rp() -> None:
    assert resolve_base_rp(None, default_base_rp=3, allowed_base_rp=(3, 5)) == 3
    assert resolve_base_rp(5, default_base_rp=3, allowed_base_rp=(3, 5)) == 5
    with pytest.raises(ValueError, match="one of: 3, 5"):
        resolve_base_rp(4, default_base_rp=3, allowed_base_rp=(3, 5))


# --- rotating codes ------------------------------------------------------------------

SECRET = bytes(range(32))
T0 = datetime(2026, 11, 1, 14, 0, 30, tzinfo=UTC)


def test_code_shape_and_alphabet() -> None:
    code = codes.current_code(SECRET, T0, 60)
    assert len(code) == codes.CODE_LENGTH
    assert set(code) <= set(codes.ALPHABET)
    assert not set("01ILO") & set(codes.ALPHABET)


def test_code_rotates_each_period_and_is_deterministic() -> None:
    a = codes.current_code(SECRET, T0, 60)
    assert codes.current_code(SECRET, T0 + timedelta(seconds=10), 60) == a
    assert codes.current_code(SECRET, T0 + timedelta(seconds=60), 60) != a
    assert codes.current_code(bytes(32), T0, 60) != a


def test_current_and_previous_step_accepted_older_rejected() -> None:
    code = codes.current_code(SECRET, T0, 60)
    assert codes.matches(SECRET, code, T0, 60)
    assert codes.matches(SECRET, code, T0 + timedelta(seconds=60), 60)
    assert not codes.matches(SECRET, code, T0 + timedelta(seconds=120), 60)
    assert not codes.matches(SECRET, code, T0 - timedelta(seconds=60), 60)


def test_code_input_is_forgiving_but_exact() -> None:
    code = codes.current_code(SECRET, T0, 60)
    typed = f" {code[:3].lower()}-{code[3:]} "
    assert codes.matches(SECRET, typed, T0, 60)
    assert not codes.matches(SECRET, code[:-1], T0, 60)
    assert not codes.matches(SECRET, "", T0, 60)


def test_seconds_left() -> None:
    assert codes.seconds_left(T0, 60) == 30


def test_codes_require_aware_datetimes() -> None:
    with pytest.raises(ValueError, match="naive"):
        codes.current_code(SECRET, datetime(2026, 11, 1), 60)  # noqa: DTZ001
