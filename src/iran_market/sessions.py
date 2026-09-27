"""Market sessions / trading calendar.

Session windows come from configuration, never hardcoded assumptions.
Default TSE cash-equity session: 09:00-12:30 Tehran, Saturday-Wednesday
(Thursday often has limited trading; Friday closed). Calendar for weekly
holidays is intentionally config-driven and later overridable by a
market-status provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .timeutil import TEHRAN


@dataclass(frozen=True)
class SessionWindow:
    """One continuous trading window on a specific date, Tehran-local."""

    start: datetime  # tz-aware Tehran
    end: datetime  # tz-aware Tehran

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


@dataclass
class SessionConfig:
    """Weekly template + overrides. All times Tehran-local.

    weekdays: {python weekday (0=Mon..6=Sun): [(HH:MM, HH:MM), ...]}
    holidays: {ISO date string: 'reason'} - no sessions that day
    """

    weekdays: dict[int, list[tuple[str, str]]] = field(
        default_factory=lambda: {
            5: [("09:00", "12:30")],  # Saturday
            6: [("09:00", "12:30")],  # Sunday
            0: [("09:00", "12:30")],  # Monday
            1: [("09:00", "12:30")],  # Tuesday
            2: [("09:00", "12:30")],  # Wednesday
        }
    )
    holidays: dict[str, str] = field(default_factory=dict)
    timezone: ZoneInfo = TEHRAN

    @classmethod
    def from_dict(cls, d: dict) -> "SessionConfig":
        return cls(
            weekdays={int(k): [(a, b) for a, b in v] for k, v in d.get("weekdays", {}).items()},
            holidays=dict(d.get("holidays", {})),
            timezone=TEHRAN,
        )

    def to_json(self) -> str:
        return json.dumps(
            {"weekdays": {str(k): v for k, v in self.weekdays.items()}, "holidays": self.holidays},
            ensure_ascii=False,
        )


class TradingCalendar:
    """Resolves concrete SessionWindows for dates from a SessionConfig."""

    def __init__(self, config: SessionConfig | None = None) -> None:
        self.config = config or SessionConfig()

    def is_trading_day(self, d: date) -> bool:
        if d.isoformat() in self.config.holidays:
            return False
        return self.config.weekdays.get(d.weekday()) is not None

    def sessions(self, d: date) -> list[SessionWindow]:
        """Concrete windows for the date (empty list => market closed)."""
        if d.isoformat() in self.config.holidays:
            return []
        windows = []
        for start_s, end_s in self.config.weekdays.get(d.weekday(), []):
            h1, m1 = map(int, start_s.split(":"))
            h2, m2 = map(int, end_s.split(":"))
            start = datetime(d.year, d.month, d.day, h1, m1, tzinfo=self.config.timezone)
            end = datetime(d.year, d.month, d.day, h2, m2, tzinfo=self.config.timezone)
            windows.append(SessionWindow(start, end))
        return windows

    def primary_session(self, d: date) -> SessionWindow | None:
        ws = self.sessions(d)
        return ws[0] if ws else None

    def hour_boundaries(self, d: date, hour: int = 1) -> list[tuple[datetime, datetime]]:
        """Session-anchored hourly bucket edges for the date.

        Buckets tile each session window in `hour` increments; the last bucket
        of a session absorbs the remainder (e.g. 09:00-12:30 -> 09:00, 10:00,
        11:00, 12:00-12:30). Not UTC-aligned.
        """
        buckets: list[tuple[datetime, datetime]] = []
        step = timedelta(hours=hour)
        for win in self.sessions(d):
            cursor = win.start
            while cursor < win.end:
                nxt = min(cursor + step, win.end)
                buckets.append((cursor, nxt))
                cursor = nxt
        return buckets

    def minute_boundaries(self, d: date, minutes: int) -> list[tuple[datetime, datetime]]:
        step = timedelta(minutes=minutes)
        return self._generic_boundaries(d, step)

    def day_window(self, d: date) -> SessionWindow | None:
        ws = self.sessions(d)
        if not ws:
            return None
        return SessionWindow(ws[0].start, ws[-1].end)

    def _generic_boundaries(self, d: date, step: timedelta):
        buckets = []
        for win in self.sessions(d):
            cursor = win.start
            while cursor < win.end:
                nxt = min(cursor + step, win.end)
                buckets.append((cursor, nxt))
                cursor = nxt
        return buckets
