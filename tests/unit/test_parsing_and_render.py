from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from rrc_bot.bot import render
from rrc_bot.bot.commands.admin import setup_warnings
from rrc_bot.bot.parsing import parse_clock, parse_day
from rrc_bot.domain.ranks import Criterion, RankProgress
from rrc_bot.domain.rules import default_rule_set
from rrc_bot.domain.timeutil import PHOENIX
from rrc_bot.services.checkin import CheckinResult
from rrc_bot.services.common import GuildSettings, ServiceError
from rrc_bot.services.events import CodeView, EventView
from rrc_bot.services.profile import Profile, RecentEntry

NOW = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)  # Sat Nov 7, 7:00 AM Phoenix
SETTINGS = GuildSettings(
    guild_id=1,
    tz=PHOENIX,
    anchor_day=date(2026, 11, 1),
    role_ids={"initiate": 5, "rrc_staff": 6},
    channel_ids={"admin_log": 7},
    rule_set_id=1,
    rule_set_version=1,
    rules=default_rule_set(),
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-21", date(2026, 11, 21)),
        ("2026-1-5", date(2026, 1, 5)),
        ("11/21", date(2026, 11, 21)),
        ("11/7", date(2026, 11, 7)),  # today counts
        ("11/6", date(2027, 11, 6)),  # already past -> next year
        ("1/3/27", date(2027, 1, 3)),
        ("today", date(2026, 11, 7)),
        (" Tomorrow ", date(2026, 11, 8)),
    ],
)
def test_parse_day(text: str, expected: date) -> None:
    assert parse_day(text, now=NOW, tz=PHOENIX) == expected


@pytest.mark.parametrize("text", ["", "next friday", "13/45", "2026-02-30", "11-21"])
def test_parse_day_rejects(text: str) -> None:
    with pytest.raises(ServiceError, match="couldn't read the date"):
        parse_day(text, now=NOW, tz=PHOENIX)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7", time(7)),
        ("7am", time(7)),
        ("7:30 PM", time(19, 30)),
        ("7:30 p.m.", time(19, 30)),
        ("12am", time(0)),
        ("12:15pm", time(12, 15)),
        ("07:05", time(7, 5)),
        ("19:30", time(19, 30)),
    ],
)
def test_parse_clock(text: str, expected: time) -> None:
    assert parse_clock(text) == expected


@pytest.mark.parametrize("text", ["", "25:00", "7:75", "13pm", "noon", "0am"])
def test_parse_clock_rejects(text: str) -> None:
    with pytest.raises(ServiceError, match="couldn't read the time"):
        parse_clock(text)


def event(**overrides: object) -> EventView:
    base: dict[str, object] = {
        "id": 3,
        "title": "Saturday Flagship",
        "type_key": "flagship",
        "type_name": "Flagship run",
        "starts_at": NOW + timedelta(minutes=10),
        "ends_at": NOW + timedelta(minutes=70),
        "location": "Papago Park",
        "base_rp": 10,
        "status": "scheduled",
        "checkin_opens_at": NOW - timedelta(minutes=20),
        "checkin_closes_at": NOW + timedelta(minutes=100),
        "group_key": None,
        "cycle_seq": 0,
    }
    base.update(overrides)
    return EventView(**base)  # type: ignore[arg-type]


def test_events_list_shows_open_checkin_and_cycle_end() -> None:
    text = render.events_list([event()], SETTINGS, NOW)
    assert "**Saturday Flagship** (Flagship run, 10 RP) · Sat Nov 7, 7:10 AM · Papago Park" in text
    assert "check-in open now" in text
    assert "This cycle (cycle 1) runs through **Sat Nov 14**." in text
    assert "Nothing scheduled" in render.events_list([], SETTINGS, NOW)


def test_cycle_line_before_launch_and_on_boundaries() -> None:
    launch = GuildSettings(**{**SETTINGS.__dict__, "anchor_day": date(2026, 11, 6)})
    before = datetime(2026, 10, 27, 20, 0, tzinfo=UTC)
    assert render.cycle_line(launch, before) == "Cycle 1 starts **Fri Nov 6**."
    first_minute = datetime(2026, 11, 6, 7, 0, tzinfo=UTC)  # 00:00 Phoenix
    assert render.cycle_line(launch, first_minute) == (
        "This cycle (cycle 1) runs through **Thu Nov 19**."
    )
    second = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    assert render.cycle_line(launch, second) == "This cycle (cycle 2) runs through **Thu Dec 3**."


def test_join_prompt_lists_coming_soon_without_terms() -> None:
    text = render.join_prompt(SETTINGS)
    assert "**Coming soon**" in text and "RRC Marketplace" in text
    assert "show up 3 cycles in a row" in text
    assert "You start with 1 streak shield." in text
    assert "PROPOSED" not in text  # internal label stays internal
    assert "%" not in text and "$" not in text  # no offer terms before they're live
    assert len(text) <= render.DISCORD_LIMIT


@pytest.mark.parametrize(
    ("outcome", "snippet"),
    [
        ("pending", "Staff confirm the roster"),
        ("already", "already checked in"),
        ("bad_code", "isn't valid right now"),
        ("rate_limited", "wait 10 minutes"),
        ("group_taken", "counts together"),
    ],
)
def test_checkin_replies(outcome: str, snippet: str) -> None:
    result = CheckinResult(outcome, event())  # type: ignore[arg-type]
    assert snippet in render.checkin_reply(result, SETTINGS)


def test_code_text_warns_when_window_closed() -> None:
    open_code = CodeView(event(), "K7QP2M", 42, 60)
    assert "`K7QP2M`" in render.code_text(open_code, SETTINGS, NOW)
    assert "⚠️" not in render.code_text(open_code, SETTINGS, NOW)
    early = render.code_text(open_code, SETTINGS, NOW - timedelta(hours=1))
    assert "opens at 6:40 AM" in early and "/rrc-admin event open" in early
    late = render.code_text(open_code, SETTINGS, NOW + timedelta(hours=3))
    assert "has closed" in late


def test_profile_text() -> None:
    p = Profile(
        enrolled_at=NOW,
        lifetime_rp=25,
        rank="initiate",
        shields=1,
        shield_cap=2,
        streak_count=0,
        hot=False,
        activation_cycles=3,
        pending_checkins=1,
        inperson_runs=2,
        next_rank=RankProgress(
            "relentless",
            (Criterion("RP", 25, 150), Criterion("in-person runs", 5, 4)),
        ),
        recent=[RecentEntry(NOW, 10, "Saturday Flagship")],
    )
    text = render.profile_text(p, SETTINGS)
    assert "Rank: **Initiate** · member since Sat Nov 7" in text
    assert "Lifetime: **25 RP**" in text
    assert "Shields: 1/2" in text
    assert "waiting for staff: 1" in text
    assert "• RP: 25/150\n• in-person runs: 4/4 ✅" in text
    assert "+10 RP · Saturday Flagship · Sat Nov 7" in text


def test_setup_text_and_warnings() -> None:
    me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True), top_role=10)
    channel = SimpleNamespace(
        mention="#admin-log",
        permissions_for=lambda _: SimpleNamespace(view_channel=True, send_messages=False),
    )
    guild = SimpleNamespace(
        me=me, get_role=lambda rid: 5 if rid == 5 else None, get_channel=lambda cid: channel
    )
    warnings = setup_warnings(guild, SETTINGS)
    assert warnings == ["I can't post in #admin-log (admin log); allow View and Send."]
    me.top_role = 3  # bot role below the Initiate role
    assert any("above** the Initiate role" in w for w in setup_warnings(guild, SETTINGS))
    text = render.setup_text(SETTINGS, [])
    assert "Cycle 1 starts: Sun Nov 1, 12:00 AM" in text
    assert "Initiate role: <@&5>" in text and "Admin role: _not set_" in text
    assert "looks good" in text


def test_long_messages_are_clipped() -> None:
    assert len(render.clip("x" * 5000)) <= render.DISCORD_LIMIT
