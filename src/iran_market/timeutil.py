"""Timezone-aware helpers. Everything internal is Asia/Tehran aware datetimes."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")


def now_tehran() -> datetime:
    """Current time in Tehran (tz-aware)."""
    return datetime.now(TEHRAN)


def tehran_midnight(d: date) -> datetime:
    """00:00 Tehran on the given date, tz-aware."""
    return datetime(d.year, d.month, d.day, tzinfo=TEHRAN)


def parse_heven(value: int | str) -> tuple[int, int, int]:
    """Parse TSETMC hEven (HHMMSS int, may lose leading zero) to (h, m, s).

    Examples: 90102 -> (9, 1, 2) i.e. 09:01:02, 122959 -> (12, 29, 59).
    """
    text = str(value).zfill(6)
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"invalid hEven: {value!r}")
    return int(text[0:2]), int(text[2:4]), int(text[4:6])


def heven_to_time(value: int | str) -> datetime:
    """Combine hEven with a trading date into a tz-aware Tehran datetime.

    Seconds may exceed 59 in upstream data (e.g. 59->60 rollover); normalize
    by carrying into minutes via timedelta.
    """
    from datetime import timedelta

    h, m, s = parse_heven(value)
    base = tehran_midnight(date(2000, 1, 1))  # any date; caller overrides
    t = base + timedelta(hours=h, minutes=m, seconds=s)
    return base.replace(hour=t.hour, minute=t.minute, second=t.second)


def to_gregorian_compact(d: date) -> str:
    """date -> 'YYYYMMDD' for API path segments."""
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"
