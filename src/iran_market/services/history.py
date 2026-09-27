"""TradeHistoryCollector: authoritative instrument-day trade fetch.

Bounded concurrency, idempotent persistence, fetch bookkeeping, resume.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date

from ..errors import IranMarketError, ProviderError
from ..http import HttpClient
from ..logging_setup import get_logger
from ..models import Trade
from ..providers.base import MarketDataProvider
from ..storage.base import FetchRunRepository, InstrumentRepository, TradeRepository
from ..storage.archive import RawArchive

log = get_logger("history")


@dataclass
class FetchOutcome:
    ins_code: str
    day: date
    status: str  # 'OK' | 'SKIPPED_DONE' | 'FAILED'
    trades: int = 0
    error: str | None = None
    duration_ms: int = 0


class TradeHistoryCollector:
    """Fetches + normalizes + persists instrument-day trade histories.

    Idempotency: repository UPSERT on (instrument, date, sequence); optional
    skip-if-done via FetchRunRepository.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        http: HttpClient,
        instruments_repo: InstrumentRepository,
        trades_repo: TradeRepository,
        fetch_repo: FetchRunRepository | None = None,
        archive: RawArchive | None = None,
        concurrency: int = 5,
        skip_already_done: bool = False,
    ) -> None:
        self._provider = provider
        self._http = http
        self._instruments = instruments_repo
        self._trades_repo = trades_repo
        self._fetch_repo = fetch_repo
        self._archive = archive
        self._sem = asyncio.Semaphore(concurrency)
        self._skip_done = skip_already_done

    async def fetch_day(
        self,
        ins_code: str,
        day: date,
        instrument_id=None,
    ) -> FetchOutcome:
        """One instrument-day. Safe to call concurrently; bounded by semaphore."""
        started = time.monotonic()
        op = f"get_trades:{ins_code}"
        fetch_id = self._fetch_repo.start(self._provider.name, "get_trades", ins_code, day) if self._fetch_repo else None

        if fetch_id is not None and self._skip_done and self._fetch_repo.has_success(
            self._provider.name, "get_trades", ins_code, day
        ):
            if self._fetch_repo:
                self._fetch_repo.finish(fetch_id, status="SKIPPED_DONE", duration_ms=0)
            return FetchOutcome(ins_code=ins_code, day=day, status="SKIPPED_DONE")

        try:
            async with self._sem:
                trades = await self._provider.get_trades(ins_code, day)
            if instrument_id is None:
                inst = self._instruments.get_by_ins_code(ins_code)
                instrument_id = inst.id if inst else trades[0].instrument_id if trades else None
            if instrument_id is not None and trades:
                for t in trades:
                    t.instrument_id = instrument_id
                self._trades_repo.upsert_many(trades)
            duration_ms = int((time.monotonic() - started) * 1000)
            if self._fetch_repo and fetch_id is not None:
                self._fetch_repo.finish(
                    fetch_id, status="OK", records_received=len(trades),
                    duration_ms=duration_ms,
                )
            log.info("trades fetched", extra={"ins": ins_code, "date": day.isoformat(), "n": len(trades), "ms": duration_ms})
            return FetchOutcome(ins_code=ins_code, day=day, status="OK", trades=len(trades), duration_ms=duration_ms)
        except IranMarketError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if self._fetch_repo and fetch_id is not None:
                self._fetch_repo.finish(fetch_id, status="FAILED", error=f"{type(exc).__name__}: {exc}", duration_ms=duration_ms)
            log.error("trade fetch failed", extra={"ins": ins_code, "date": day.isoformat(), "error": str(exc)})
            return FetchOutcome(ins_code=ins_code, day=day, status="FAILED", error=str(exc), duration_ms=duration_ms)

    async def fetch_many(self, requests: list[tuple[str, date]]) -> list[FetchOutcome]:
        """Concurrent instrument-day fetches, bounded by the semaphore."""
        tasks = [self.fetch_day(ins, day) for ins, day in requests]
        return list(await asyncio.gather(*tasks))
