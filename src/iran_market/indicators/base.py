"""Generic indicator framework.

Indicators: deterministic math over normalized CLOSED candles. No provider,
storage, or alerting dependencies. Consumers (scanners) decide what a state
MEANS; indicators only report state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ..models import Candle, CandleStatus


@dataclass(frozen=True)
class IndicatorResult:
    """One indicator evaluation over a candle series."""

    instrument_id: Any
    timeframe: str
    indicator_name: str
    parameters: dict[str, Any]
    candle_time: Any  # datetime of the last candle used
    values: dict[str, Any]  # indicator-specific, e.g. {'supertrend': ..., 'atr': ...}
    state: str | None  # e.g. 'BULLISH'/'BEARISH'/'UNKNOWN', or None
    valid: bool  # False during warmup / insufficient history
    metadata: dict[str, Any] = field(default_factory=dict)


class Indicator(Protocol):
    name: str

    def required_history(self) -> int:
        """Minimum CLOSED candles needed before results are valid (excl. warmup)."""
        ...

    def calculate(self, candles: Sequence[Candle]) -> IndicatorResult:
        """Pure function: same candles -> same result. No hidden state."""
        ...


def closed_candles(candles: Sequence[Candle]) -> list[Candle]:
    """Filter to CLOSED/RECONCILED only. BUILDING candles never feed indicators."""
    return [
        c for c in candles
        if c.status in (CandleStatus.CLOSED, CandleStatus.RECONCILED)
    ]


def true_range(prev_close: float | None, high: float, low: float, close: float) -> float:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|). First bar: high-low."""
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def wilder_atr(
    candles: Sequence[Candle], period: int
) -> tuple[list[float], list[bool]]:
    """Wilder's RMA ATR (SMMA). Returns (atr_values, valid_flags) per candle.

    Initialization: ATR[period-1] = SMA of TR over first `period` TRs
    (standard Wilder seeding). Then ATR[i] = (ATR[i-1]*(period-1) + TR[i]) / period.
    valid[i] is False for i < period-1. Deterministic float math; values are
    NOT rounded (rounding is display-only, downstream).
    """
    if period < 1:
        raise ValueError("ATR period must be >= 1")
    n = len(candles)
    atr: list[float] = [math.nan] * n
    valid: list[bool] = [False] * n
    if n == 0:
        return atr, valid

    trs: list[float] = []
    prev_close: float | None = None
    for c in candles:
        trs.append(true_range(prev_close, float(c.high), float(c.low), float(c.close)))
        prev_close = float(c.close)

    if n < period:
        return atr, valid

    atr[period - 1] = sum(trs[:period]) / period
    valid[period - 1] = True
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
        valid[i] = True
    return atr, valid
