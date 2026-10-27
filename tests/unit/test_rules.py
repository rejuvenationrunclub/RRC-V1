from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from rrc_bot.domain.rules import RuleSet, default_rule_set, load_rule_set


def test_default_rules_match_plan_and_are_labelled_proposed() -> None:
    rules = default_rule_set()
    assert "PROPOSED" in rules.label
    assert rules.cycle_length_days == 14
    assert rules.rp.flagship == 10
    assert rules.rp.popup_allowed == (3, 5)
    assert (rules.rp.proof_per_day, rules.rp.proof_week_cap) == (1, 7)
    assert rules.streak.activation_cycles == 3
    assert rules.streak.multiplier == Decimal("1.5")
    assert rules.streak.rounding == "half_up"
    assert rules.shields.enrollment_grant == 1
    assert (rules.shields.grant_every_rp, rules.shields.max_balance) == (100, 2)
    assert rules.shields.allow_consecutive is False
    assert rules.ranks.relentless.min_rp == 150
    assert rules.ranks.legacy.min_inperson_in_52w == 18


def test_json_round_trip_is_exact() -> None:
    rules = default_rule_set()
    data = rules.to_json()
    assert data["streak"]["multiplier"] == "1.5"  # type: ignore[index]
    assert RuleSet.model_validate(data) == rules


def test_float_multiplier_is_read_exactly() -> None:
    text = default_rule_set().model_dump_json().replace('"1.5"', "1.1")
    assert load_rule_set(text).streak.multiplier == Decimal("1.1")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("rp", "flagship"), 0),
        (("rp", "popup_allowed"), []),
        (("rp", "popup_allowed"), [3, -5]),
        (("streak", "multiplier"), "0.5"),
        (("streak", "rounding"), "bankers"),
        (("shields", "max_balance"), 11),
        (("shields", "enrollment_grant"), 3),
        (("cycle_length_days",), 0),
    ],
)
def test_invalid_values_rejected(path: tuple[str, ...], value: object) -> None:
    data = default_rule_set().to_json()
    target = data
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        RuleSet.model_validate(data)


def test_unknown_keys_rejected() -> None:
    data = default_rule_set().to_json()
    data["rp"]["mystery_bonus"] = 99  # type: ignore[index]
    with pytest.raises(ValidationError, match="mystery_bonus"):
        RuleSet.model_validate(data)


def test_load_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        load_rule_set("- just\n- a list\n")


def test_rule_set_is_immutable() -> None:
    rules = default_rule_set()
    with pytest.raises(ValidationError):
        rules.rp.flagship = 12  # type: ignore[misc]
