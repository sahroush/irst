"""Closed-candle scanner framework and Supertrend state-change scanner."""
from __future__ import annotations

import hashlib, json
from dataclasses import dataclass

from ..indicators.supertrend import SupertrendIndicator
from ..models import Candle, Signal
from ..storage.base import CandleRepository, SignalRepository


@dataclass(frozen=True)
class ScannerConfig:
    name: str = "supertrend"
    timeframe: str = "30m"
    period: int = 10
    multiplier: float = 3.0
    history: int = 100


class ScannerService:
    def __init__(self, candles: CandleRepository, signals: SignalRepository,
                 config: ScannerConfig | None = None, events=None):
        self._candles = candles
        self._signals = signals
        self._events = events
        self.config = config or ScannerConfig()
        self.indicator = SupertrendIndicator(self.config.period, self.config.multiplier)

    def evaluate(self, candle: Candle) -> Signal | None:
        if candle.timeframe != self.config.timeframe:
            return None
        # Evaluate the series *as of this candle*.  This matters for historical
        # replay/backfill: future candles must never influence an older signal.
        series = self._candles.get_series(
            candle.instrument_id, candle.timeframe, end=candle.end_time
        )
        series = [c for c in series if c.start_time <= candle.start_time]
        series = series[-self.config.history:]
        if len(series) < self.indicator.required_history() + 1:
            return None
        current = self.indicator.calculate(series)
        previous = self.indicator.calculate(series[:-1])
        if self._events:
            self._events.publish("IndicatorCalculated", {"result": current, "scanner": self.config.name})
        if not current.valid or not previous.valid or current.state == previous.state:
            return None
        direction = "bullish" if current.state == "BULLISH" else "bearish"
        params_json = json.dumps(self.indicator.parameters, sort_keys=True)
        raw = f"{self.config.name}|{candle.instrument_id}|{candle.timeframe}|{candle.start_time.isoformat()}|supertrend_flip|{params_json}"
        signal = Signal(
            id=hashlib.sha1(raw.encode()).hexdigest(), scanner_name=self.config.name,
            instrument_id=candle.instrument_id, timeframe=candle.timeframe,
            candle_time=candle.start_time, signal_type="supertrend_flip", direction=direction,
            indicator_name="supertrend", parameters=self.indicator.parameters,
            payload={"previous_state": previous.state, "current_state": current.state,
                     "supertrend": current.values.get("supertrend"), "atr": current.values.get("atr"),
                     "close_price": candle.close},
        )
        if not self._signals.upsert(signal):
            return None
        if self._events:
            self._events.publish("SignalGenerated", {"signal": signal, "scanner": self.config.name})
        return signal
