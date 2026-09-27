"""First-run historical bootstrap for the whole market universe.

The prefetcher intentionally reuses the authoritative TSETMC trade-history path,
CandleService and ScannerService.  It can therefore populate a fresh database at
night/weekends without depending on live WebGW snapshots.

It is resumable: TradeHistoryCollector skips instrument-days that already have a
successful fetch_run, while repository UPSERTs make candle/signal replay
idempotent.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Iterable

from ..candles.models import Timeframe
from ..candles.service import CandleService
from ..registry.sync import RegistrySyncService
from ..scanner.service import ScannerConfig, ScannerService
from ..services.history import TradeHistoryCollector
from ..timeutil import now_tehran


@dataclass
class PrefetchReport:
    instruments_selected: int = 0
    instruments_skipped_unmapped: int = 0
    candidate_sessions: int = 0
    instrument_days_requested: int = 0
    fetched_ok: int = 0
    skipped_done: int = 0
    failed: int = 0
    trades_received: int = 0
    candles_built: int = 0
    signals_generated: int = 0
    start_date: str | None = None
    end_date: str | None = None
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class PrefetchService:
    """Populate historical candles/signals for a fresh installation."""

    def __init__(self, container) -> None:
        self.c = container

    async def run(
        self,
        *,
        sessions: int = 30,
        start: date | None = None,
        end: date | None = None,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
        signal_timeframe: str = "1h",
        period: int = 10,
        multiplier: float = 3.0,
        sync_registry: bool = True,
        include_inactive: bool = False,
        replay_signals: bool = True,
        batch_size: int = 250,
        progress=None,
    ) -> PrefetchReport:
        if sessions < 1 and start is None:
            raise ValueError("sessions must be >= 1 when --start is not supplied")
        if start and end and start > end:
            raise ValueError("start date must be <= end date")

        if sync_registry:
            await RegistrySyncService(
                self.c.webgw, self.c.legacy, self.c.instruments_repo
            ).sync()

        instruments, skipped_unmapped = self._select_instruments(
            symbols=symbols, include_inactive=include_inactive
        )
        if not instruments:
            raise RuntimeError(
                "No TSETMC-mapped instruments selected. Run registry-sync and "
                "check that the legacy InsCode mapping endpoint is reachable."
            )

        days = self._candidate_days(sessions=sessions, start=start, end=end)
        if not days:
            raise RuntimeError("No candidate trading sessions in requested range")

        report = PrefetchReport(
            instruments_selected=len(instruments),
            instruments_skipped_unmapped=skipped_unmapped,
            candidate_sessions=len(days),
            instrument_days_requested=len(instruments) * len(days),
            start_date=days[0].isoformat(),
            end_date=days[-1].isoformat(),
        )

        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        history = TradeHistoryCollector(
            self.c.tsetmc,
            self.c.http_tsetmc,
            self.c.instruments_repo,
            self.c.trades_repo,
            self.c.fetch_repo,
            archive=self.c.archive,
            concurrency=self.c.settings.http_concurrency,
            skip_already_done=True,
        )
        requests = [(inst.ins_code, day) for inst in instruments for day in days]
        outcomes = []
        total = len(requests)
        # Do not create tens of thousands of asyncio Tasks at once on a large
        # first run. Network concurrency is separately bounded by the collector;
        # this batch cap bounds task/memory pressure and gives useful progress.
        for offset in range(0, total, batch_size):
            batch = requests[offset:offset + batch_size]
            batch_outcomes = await history.fetch_many(batch)
            outcomes.extend(batch_outcomes)
            if progress is not None:
                progress(min(offset + len(batch), total), total, batch_outcomes)

        by_key = {(o.ins_code, o.day): o for o in outcomes}
        report.fetched_ok = sum(o.status == "OK" for o in outcomes)
        report.skipped_done = sum(o.status == "SKIPPED_DONE" for o in outcomes)
        report.failed = sum(o.status == "FAILED" for o in outcomes)
        report.trades_received = sum(o.trades for o in outcomes if o.status == "OK")
        report.failures = [
            {"ins_code": o.ins_code, "date": o.day.isoformat(), "error": o.error}
            for o in outcomes if o.status == "FAILED"
        ][:100]

        candle_service = CandleService(
            self.c.trades_repo,
            self.c.candles_repo,
            self.c.instruments_repo,
            self.c.calendar,
            events=self.c.events,
        )
        tfs = [Timeframe(x) for x in (timeframes or ["1h"])]

        # Build candles oldest -> newest.  SKIPPED_DONE is deliberately included:
        # the fetch may already exist while candles are missing after an interrupted run.
        for inst in instruments:
            for day in days:
                outcome = by_key.get((inst.ins_code, day))
                if outcome and outcome.status == "FAILED":
                    continue
                trades = self.c.trades_repo.get_trades(inst.id, day)
                if not trades:
                    continue
                for tf in tfs:
                    candles = candle_service.build_and_store(inst.id, day, tf, trades=trades)
                    report.candles_built += len(candles)

        if replay_signals and signal_timeframe in {tf.code for tf in tfs}:
            scanner = ScannerService(
                self.c.candles_repo,
                self.c.signals_repo,
                ScannerConfig(
                    timeframe=signal_timeframe,
                    period=period,
                    multiplier=multiplier,
                ),
                events=self.c.events,
            )
            # Replay every selected candle chronologically. ScannerService evaluates
            # strictly "as of" each candle, so no future leakage occurs.
            for inst in instruments:
                series = self.c.candles_repo.get_series(inst.id, signal_timeframe)
                for candle in series:
                    if scanner.evaluate(candle) is not None:
                        report.signals_generated += 1

        return report

    def _select_instruments(self, *, symbols: list[str] | None,
                            include_inactive: bool) -> tuple[list, int]:
        if symbols:
            selected = []
            seen = set()
            for symbol in symbols:
                for inst in self.c.instruments_repo.get_by_symbol(symbol):
                    if inst.id in seen:
                        continue
                    seen.add(inst.id)
                    if include_inactive or inst.active:
                        selected.append(inst)
        else:
            # Repository currently exposes list_active only.  First-run prefetch is
            # intentionally active-universe focused; include_inactive is meaningful
            # only for explicitly named symbols until a list_all API is added.
            selected = self.c.instruments_repo.list_active()

        mapped = [x for x in selected if x.ins_code]
        return mapped, len(selected) - len(mapped)

    def _candidate_days(self, *, sessions: int, start: date | None,
                        end: date | None) -> list[date]:
        end = end or now_tehran().date()
        if start is not None:
            days = []
            cursor = start
            while cursor <= end:
                if self.c.calendar.is_trading_day(cursor):
                    days.append(cursor)
                cursor += timedelta(days=1)
            return days

        days: list[date] = []
        cursor = end
        # `sessions` means configured trading-session candidates, not calendar days.
        while len(days) < sessions:
            if self.c.calendar.is_trading_day(cursor):
                days.append(cursor)
            cursor -= timedelta(days=1)
        days.sort()
        return days
