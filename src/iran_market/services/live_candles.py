"""Live/provisional candles built from cumulative market-watch snapshots."""
from __future__ import annotations

from datetime import date, datetime

from ..candles.models import Timeframe
from ..events import EventBus
from ..models import Candle, CandleStatus, LiveSnapshotState, MarketSnapshot
from ..sessions import TradingCalendar
from ..storage.base import CandleRepository, LiveStateRepository
from ..timeutil import now_tehran


class LiveCandleService:
    def __init__(self, candles: CandleRepository, live_state: LiveStateRepository,
                 calendar: TradingCalendar, events: EventBus | None = None):
        self._candles = candles
        self._state = live_state
        self._calendar = calendar
        self._events = events

    def process(self, snapshot: MarketSnapshot, timeframe: Timeframe) -> Candle | None:
        if snapshot.instrument_id is None or snapshot.last_price is None:
            return None
        bucket = self._bucket(snapshot.timestamp, timeframe)
        if bucket is None:
            self._remember(snapshot)
            return None
        start, end = bucket
        previous = self._state.get(snapshot.instrument_id)
        if previous and snapshot.timestamp < previous.timestamp:
            return None

        volume_delta = self._delta(snapshot.cumulative_volume, previous.cumulative_volume if previous else None)
        trades_delta = self._delta(snapshot.trade_count, previous.trade_count if previous else None)

        current = self._candles.get(snapshot.instrument_id, timeframe.code, start)
        if current is None:
            current = Candle(
                instrument_id=snapshot.instrument_id, timeframe=timeframe.code,
                start_time=start, end_time=end, open=snapshot.last_price,
                high=snapshot.last_price, low=snapshot.last_price, close=snapshot.last_price,
                volume=volume_delta, trade_count=trades_delta,
                status=CandleStatus.BUILDING, source="tse_webgw",
            )
        else:
            current.high = max(current.high, snapshot.last_price)
            current.low = min(current.low, snapshot.last_price)
            current.close = snapshot.last_price
            current.volume += volume_delta
            current.trade_count += trades_delta
            current.updated_at = now_tehran()
        self._candles.upsert(current)
        self._remember(snapshot)
        if self._events:
            self._events.publish("CandleUpdated", {"candle": current, "timeframe": timeframe.code})
        return current

    def close_elapsed(self, now: datetime, timeframe: Timeframe) -> list[Candle]:
        """Close BUILDING candles whose bucket end is <= now; emit exactly once."""
        conn = getattr(self._candles, "_store", None)
        if conn is None:
            return []
        db = conn.connect()
        rows = db.execute(
            "SELECT * FROM candles WHERE timeframe=? AND status='BUILDING' AND end_time <= ? ORDER BY end_time",
            (timeframe.code, now.isoformat()),
        ).fetchall()
        closed = []
        for r in rows:
            c = self._candles._row_to_candle(r)
            c.status = CandleStatus.CLOSED
            c.updated_at = now_tehran()
            self._candles.upsert(c)
            closed.append(c)
            if self._events:
                self._events.publish("CandleClosed", {"candle": c, "timeframe": timeframe.code})
        return closed

    def close_session_candles(self, day: date, timeframe: Timeframe) -> list[Candle]:
        window = self._calendar.day_window(day)
        return [] if window is None else self.close_elapsed(window.end, timeframe)

    def _bucket(self, ts: datetime, timeframe: Timeframe):
        d = ts.date()
        if timeframe.is_daily:
            win = self._calendar.day_window(d)
            return None if win is None or not win.contains(ts) else (win.start, win.end)
        edges = (self._calendar.hour_boundaries(d, timeframe.amount) if timeframe.unit == "h"
                 else self._calendar.minute_boundaries(d, timeframe.amount))
        for start, end in edges:
            if start <= ts < end:
                return start, end
        return None

    @staticmethod
    def _delta(current: int | None, previous: int | None) -> int:
        if current is None or previous is None:
            return 0
        return max(0, current - previous)

    def _remember(self, s: MarketSnapshot) -> None:
        self._state.upsert(LiveSnapshotState(
            instrument_id=s.instrument_id, timestamp=s.timestamp,
            cumulative_volume=s.cumulative_volume, cumulative_value=s.cumulative_value,
            trade_count=s.trade_count, last_price=s.last_price,
        ))
