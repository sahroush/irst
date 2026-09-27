"""Timeframe representation: generic, not per-interval functions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Timeframe:
    """A bar length: '1m', '5m', '15m', '1h', '1d' (calendar day for '1d')."""

    code: str

    _PATTERN = re.compile(r"^(\d+)([mhd])$")

    def __post_init__(self) -> None:
        m = self._PATTERN.match(self.code)
        if not m:
            raise ValueError(f"invalid timeframe: {self.code!r}")
        object.__setattr__(self, "amount", int(m.group(1)))
        object.__setattr__(self, "unit", m.group(2))
        if self.amount == 0:
            raise ValueError("timeframe amount must be > 0")
        if self.unit == "m" and self.amount > 1440:
            raise ValueError("minutes too large; use 'h' or 'd'")
        if self.unit == "h" and self.amount > 48:
            raise ValueError("hours too large; use 'd'")

    @property
    def timedelta(self) -> timedelta:
        if self.unit == "m":
            return timedelta(minutes=self.amount)
        if self.unit == "h":
            return timedelta(hours=self.amount)
        return timedelta(days=self.amount)

    @property
    def is_daily(self) -> bool:
        return self.unit == "d"

    def __str__(self) -> str:  # pragma: no cover
        return self.code


ONE_MIN = Timeframe("1m")
FIVE_MIN = Timeframe("5m")
FIFTEEN_MIN = Timeframe("15m")
THIRTY_MIN = Timeframe("30m")
ONE_HOUR = Timeframe("1h")
ONE_DAY = Timeframe("1d")
