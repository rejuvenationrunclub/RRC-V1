from __future__ import annotations

from datetime import UTC, date, datetime

from rrc_bot.services.common import Actor, capabilities, jsonable
from rrc_bot.services.outbox import backoff


def test_capabilities_map_roles() -> None:
    role_map = {"rrc_admin": 10, "rrc_staff": 20}
    assert capabilities(role_ids=set(), manage_guild=True, role_map={}) == {"admin", "staff"}
    assert capabilities(role_ids={10}, manage_guild=False, role_map=role_map) == {"admin", "staff"}
    assert capabilities(role_ids={20}, manage_guild=False, role_map=role_map) == {"staff"}
    assert capabilities(role_ids={30}, manage_guild=False, role_map=role_map) == set()
    assert capabilities(role_ids={10}, manage_guild=False, role_map={}) == set()


def test_actor_capabilities() -> None:
    assert Actor(1, capabilities=frozenset({"admin"})).is_staff
    assert not Actor(1).is_staff and not Actor(1).is_admin


def test_backoff_grows_and_caps() -> None:
    assert [backoff(n).total_seconds() for n in (1, 2, 3)] == [5, 10, 20]
    assert backoff(30).total_seconds() == 3600


def test_jsonable_handles_dates() -> None:
    when = datetime(2026, 11, 1, 7, tzinfo=UTC)
    assert jsonable({"at": when, "days": (date(2026, 11, 1),), 1: "x"}) == {
        "at": "2026-11-01T07:00:00+00:00",
        "days": ["2026-11-01"],
        "1": "x",
    }
