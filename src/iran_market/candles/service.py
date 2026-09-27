"""Candle service: orchestrates trades -> candles -> storage (UPSERT, idempotent)."""

from __future__ import annotations

from datetime import date

from ..events import EventBus
from ..logging_setup import get_logger
from ..models import Candle, CandleStatus, Trade
from ..sessions import TradingCalendar
from ..storage.base import CandleRepository, InstrumentRepository, TradeRepository
from .builder import CandleBuilder
from .models import Timeframe

log = get_logger("candle_service")


class CandleService:
    """Builds exact candles from stored trades and persists them (UPSERT)."""

    def __init__(
        self,
        trades_repo: TradeRepository,
        candles_repo: CandleRepository,
        instruments_repo: InstrumentRepository,
        calendar: TradingCalendar | None = None,
        events: EventBus | None = None,
    ) -> None:
        self._trades = trades_repo
        self._candles = candles_repo
        self._instruments = instruments_repo
        self._calendar = calendar or TradingCalendar()
        self._builder = CandleBuilder()
        self._events = events

    def build_and_store(
        self,
        instrument_id,
        day: date,
        timeframe: Timeframe,
        trades: list[Trade] | None = None,
    ) -> list[Candle]:
        """Build session-anchored candles for instrument+day and UPSERT them.

        `trades` optionally injected (avoids re-fetch); else loaded from repo.
        Reconciliation and re-runs are idempotent via repository UPSERT.
        """
        if trades is None:
            trades = self._trades.get_trades(instrument_id, day)
        trades = CandleBuilder.dedupe(trades)

        edges = self._edges(day, timeframe)
        status = CandleStatus.RECONCILED if any(c.status == CandleStatus.RECONCILED for c in []) else CandleStatus.CLOSED
        candles = self._builder.build(
            trades, edges, timeframe, source="trades", status=status
        )
        for c in candles:
            self._candles.upsert(c)
        self._emit(candles, timeframe)
        return candles

    def reconcile(self, instrument_id, day: date, timeframe: Timeframe, trades: list[Trade]) -> list[Candle]:
        """Replace/validate provisional candles with trade-derived ones.

        Idempotent: same trades -> same candles -> same UPSERT result.
        Marks RECONCILED, records reconciled_at.
        """
        trades = CandleBuilder.dedupe(trades)
        edges = self._edges(day, timeframe)
        candles = self._builder.build(trades, edges, timeframe, source="trades")
        for c in candles:
            c.status = CandleStatus.RECONCILED
            c.reconciled_at = c.updated_at
            self._candles.upsert(c)
        self._emit(candles, timeframe, reconciled=True)
        log.info("reconciled", extra={"timeframe": timeframe.code, "candles": len(candles)})
        return candles

    def _edges(self, day: date, timeframe: Timeframe):
        if timeframe.is_daily:
            win = self._calendar.day_window(day)
            return [] if win is None else [(win.start, win.end)]
        if timeframe.unit == "h":
            return self._calendar.hour_boundaries(day, timeframe.amount)
        return self._calendar.minute_boundaries(day, timeframe.amount)

    def _emit(self, candles: list[Candle], timeframe: Timeframe, reconciled: bool = False) -> None:
        if self._events is None:
            return
        for c in candles:
            name = "CandleReconciled" if reconciled else "CandleClosed"
            self._events.publish(name, {"candle": c, "timeframe": timeframe.code})


from datetime import datetime  # noqa: E402  (used in reconcile)
