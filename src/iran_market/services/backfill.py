"""Backfill: multi-instrument, multi-day trade fetch + candle build, resumable."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date, timedelta

from ..candles.models import Timeframe
from ..candles.service import CandleService
from ..http import HttpClient
from ..logging_setup import get_logger
from ..providers.base import MarketDataProvider
from ..storage.base import CandleRepository, FetchRunRepository, InstrumentRepository, TradeRepository

log = get_logger("backfill")


def daterange(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


class BackfillService:
    def __init__(
        self,
        provider: MarketDataProvider,
        http: HttpClient,
        instruments_repo: InstrumentRepository,
        trades_repo: TradeRepository,
        candles_repo: CandleRepository,
        fetch_repo: FetchRunRepository,
        candle_service: CandleService | None = None,
        concurrency: int = 5,
    ) -> None:
        from .history import TradeHistoryCollector

        self._history = TradeHistoryCollector(
            provider, http, instruments_repo, trades_repo, fetch_repo,
            concurrency=concurrency, skip_already_done=True,
        )
        self._candles = candle_service or CandleService(
            trades_repo, candles_repo, instruments_repo
        )
        self._instruments = instruments_repo

    async def run(
        self,
        ins_codes: list[str],
        start: date,
        end: date,
        timeframes: list[Timeframe] | None = None,
    ) -> dict:
        """Fetch all instrument-days then build candles. Returns summary dict."""
        requests = [(ins, d) for ins in ins_codes for d in daterange(start, end)]
        outcomes = await self._history.fetch_many(requests)

        timeframes = timeframes or [Timeframe("1h")]
        built = 0
        for outcome in outcomes:
            if outcome.status != "OK" or outcome.trades == 0:
                continue
            inst = self._instruments.get_by_ins_code(outcome.ins_code)
            if inst is None:
                continue
            for tf in timeframes:
                candles = self._candles.build_and_store(inst.id, outcome.day, tf)
                built += len(candles)

        summary = {
            "days_requested": len(requests),
            "ok": sum(1 for o in outcomes if o.status == "OK"),
            "skipped": sum(1 for o in outcomes if o.status == "SKIPPED_DONE"),
            "failed": sum(1 for o in outcomes if o.status == "FAILED"),
            "candles_built": built,
        }
        log.info("backfill complete", extra=summary)
        return summary
