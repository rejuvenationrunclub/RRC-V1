"""Time helpers: UTC storage, local (Phoenix) calendar math and display.

Cycle boundaries are local midnights on calendar dates ``anchor + n * length``.
Phoenix has no DST, but computing through the local calendar keeps the math
right for any IANA zone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PHOENIX = ZoneInfo("America/Phoenix")


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime; every instant must carry a time zone")
    return value


def local_date(instant: datetime, tz: ZoneInfo) -> date:
    return require_aware(instant).astimezone(tz).date()


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    """The UTC instant of 00:00 on ``day`` in ``tz``."""
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


def local_datetime(day: date, at: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of ``at`` on ``day`` in ``tz``."""
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def cycle_seq(instant: datetime, anchor_day: date, length_days: int, tz: ZoneInfo) -> int:
    """Index of the cycle containing ``instant``; cycle 0 starts at the anchor's local midnight.

    Negative indexes are cycles before the anchor.
    """
    return (local_date(instant, tz) - anchor_day).days // length_days


def cycle_bounds(
    seq: int, anchor_day: date, length_days: int, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Half-open ``[starts_at, ends_at)`` of cycle ``seq`` as UTC instants."""
    start_day = anchor_day + timedelta(days=seq * length_days)
    return local_midnight(start_day, tz), local_midnight(
        start_day + timedelta(days=length_days), tz
    )


def week_key(day: date) -> tuple[int, int]:
    """ISO (year, week) of a local calendar date: Monday-Sunday weeks."""
    iso = day.isocalendar()
    return iso.year, iso.week


def format_local(instant: datetime, tz: ZoneInfo, *, with_date: bool = True) -> str:
    """``Sat Nov 1, 7:00 AM`` (or just ``7:00 AM``) in ``tz``, without zero padding."""
    local = require_aware(instant).astimezone(tz)
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    if not with_date:
        return clock
    return f"{local:%a} {local:%b} {local.day}, {clock}"
