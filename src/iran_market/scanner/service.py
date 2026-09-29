"""Closed-candle scanner framework and Supertrend state-change scanner."""
from __future__ import annotations

import hashlib, json
from dataclasses import dataclass

from ..indicators.base import provisional_candles
from ..indicators.supertrend import SupertrendIndicator
from ..indicators.rsi import rsi_confirms, rsi_series
from ..models import Candle, CandleStatus, Signal
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
        provisional = bool(series) and series[-1].status == CandleStatus.BUILDING
        # An in-progress candle participates on the prices observed so far, so
        # alerts can fire before the period completes.  The signal id is the
        # same as the final one: at close the provisional row is promoted to
        # final values or retracted when the flip did not survive.
        live = provisional_candles(series)
        if len(live) < self.indicator.required_history() + 1:
            return None
        current = self.indicator.calculate(live)
        previous = self.indicator.calculate(live[:-1])
        if self._events:
            self._events.publish("IndicatorCalculated", {"result": current, "scanner": self.config.name})
        signal_id = self._signal_id(candle)
        if not current.valid or not previous.valid or current.state == previous.state:
            if not provisional:
                self._resolve_provisional(signal_id, None)
            return None
        direction = "bullish" if current.state == "BULLISH" else "bearish"
        rsi = rsi_series([float(item.close) for item in live])
        current_rsi, previous_rsi = rsi[-1], rsi[-2]
        if not rsi_confirms(direction, current_rsi, previous_rsi):
            if not provisional:
                self._resolve_provisional(signal_id, None)
            return None
        signal = Signal(
            id=signal_id, scanner_name=self.config.name,
            instrument_id=candle.instrument_id, timeframe=candle.timeframe,
            candle_time=candle.start_time, signal_type="supertrend_flip", direction=direction,
            indicator_name="supertrend", parameters=self.indicator.parameters,
            payload={"previous_state": previous.state, "current_state": current.state,
                     "supertrend": current.values.get("supertrend"), "atr": current.values.get("atr"),
                     "close_price": candle.close, "rsi": current_rsi, "previous_rsi": previous_rsi,
                     "provisional": provisional},
        )
        if not self._signals.upsert(signal):
            if not provisional:
                self._resolve_provisional(signal_id, signal)
            return None
        if self._events:
            self._events.publish("SignalGenerated", {"signal": signal, "scanner": self.config.name})
        return signal

    def _signal_id(self, candle: Candle) -> str:
        params_json = json.dumps(self.indicator.parameters, sort_keys=True)
        raw = f"{self.config.name}|{candle.instrument_id}|{candle.timeframe}|{candle.start_time.isoformat()}|supertrend_flip|{params_json}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def _resolve_provisional(self, signal_id: str, final: Signal | None) -> None:
        """Retract a provisional alert (`final` is None) or promote it to final.

        No-op when the candle has no provisional row, so the common closed-
        candle path costs at most one primary-key lookup.
        """
        existing = self._signals.get(signal_id)
        if existing is None or not existing.payload.get("provisional"):
            return
        self._signals.delete(signal_id)
        if final is not None:
            self._signals.upsert(final)
