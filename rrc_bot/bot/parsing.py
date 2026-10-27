"""Parse the dates and times staff type into slash-command options (Phoenix time)."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from rrc_bot.domain.timeutil import local_date
from rrc_bot.services.common import ServiceError

_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?$", re.IGNORECASE)


def parse_day(text: str, *, now: datetime, tz: ZoneInfo) -> date:
    """``2026-11-07``, ``11/7``, ``11/7/2026``, ``today`` or ``tomorrow``."""
    raw = text.strip().lower()
    today = local_date(now, tz)
    if raw == "today":
        return today
    if raw == "tomorrow":
        return today + timedelta(days=1)
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
            return date.fromisoformat("-".join(p.zfill(2) for p in raw.split("-")))
        parts = raw.split("/")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            month, day = int(parts[0]), int(parts[1])
            if len(parts) == 3:
                year = int(parts[2])
                year += 2000 if year < 100 else 0
                return date(year, month, day)
            # No year: the next such date (today counts).
            candidate = date(today.year, month, day)
            return candidate if candidate >= today else date(today.year + 1, month, day)
    except ValueError:
        pass
    raise ServiceError(
        f"I couldn't read the date '{text}'. Try 2026-11-07, 11/7, today or tomorrow."
    )


def parse_clock(text: str) -> time:
    """``7``, ``7am``, ``7:30 pm``, ``07:30`` or ``19:30``."""
    match = _TIME.match(text.strip())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower().replace(".", "")
        if meridiem:
            if not 1 <= hour <= 12:
                hour = -1
            elif meridiem == "am":
                hour = 0 if hour == 12 else hour
            else:
                hour = 12 if hour == 12 else hour + 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    raise ServiceError(f"I couldn't read the time '{text}'. Try 7:00 AM, 7am or 19:30.")
