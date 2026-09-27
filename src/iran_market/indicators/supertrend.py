"""Deterministic Supertrend over normalized closed candles."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import Candle
from .base import IndicatorResult, closed_candles, wilder_atr


@dataclass(frozen=True)
class SupertrendIndicator:
    period: int = 10
    multiplier: float = 3.0
    name: str = "supertrend"

    def __post_init__(self):
        if self.period < 1:
            raise ValueError("period must be >= 1")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be > 0")

    def required_history(self) -> int:
        return self.period + 1

    def calculate(self, candles: Sequence[Candle]) -> IndicatorResult:
        cs = sorted(closed_candles(candles), key=lambda c: c.start_time)
        if not cs:
            return IndicatorResult(None, "", self.name, self.parameters, None, {}, "UNKNOWN", False)
        states, values = self._series(cs)
        i = len(cs) - 1
        valid = values[i] is not None
        atr, _ = wilder_atr(cs, self.period)
        return IndicatorResult(
            instrument_id=cs[i].instrument_id,
            timeframe=cs[i].timeframe,
            indicator_name=self.name,
            parameters=self.parameters,
            candle_time=cs[i].start_time,
            values={"supertrend": values[i], "atr": None if i >= len(atr) or atr[i] != atr[i] else atr[i]},
            state=states[i] if valid else "UNKNOWN",
            valid=valid,
            metadata={"warmup": self.period, "atr_method": "Wilder RMA"},
        )

    @property
    def parameters(self) -> dict:
        return {"period": self.period, "multiplier": self.multiplier}

    def _series(self, cs: Sequence[Candle]) -> tuple[list[str], list[float | None]]:
        n = len(cs)
        atr, valid_atr = wilder_atr(cs, self.period)
        final_upper = [None] * n
        final_lower = [None] * n
        st = [None] * n
        state = ["UNKNOWN"] * n

        for i, c in enumerate(cs):
            if not valid_atr[i]:
                continue
            hl2 = (float(c.high) + float(c.low)) / 2.0
            basic_upper = hl2 + self.multiplier * atr[i]
            basic_lower = hl2 - self.multiplier * atr[i]

            if i == 0 or final_upper[i - 1] is None:
                final_upper[i] = basic_upper
                final_lower[i] = basic_lower
            else:
                prev_close = float(cs[i - 1].close)
                prev_upper = final_upper[i - 1]
                prev_lower = final_lower[i - 1]
                final_upper[i] = basic_upper if (basic_upper < prev_upper or prev_close > prev_upper) else prev_upper
                final_lower[i] = basic_lower if (basic_lower > prev_lower or prev_close < prev_lower) else prev_lower

            close = float(c.close)
            if i == 0 or st[i - 1] is None or state[i - 1] == "UNKNOWN":
                state[i] = "BEARISH" if close <= final_upper[i] else "BULLISH"
            elif state[i - 1] == "BEARISH":
                state[i] = "BULLISH" if close > final_upper[i] else "BEARISH"
            else:
                state[i] = "BEARISH" if close < final_lower[i] else "BULLISH"
            st[i] = final_lower[i] if state[i] == "BULLISH" else final_upper[i]
        return state, st
