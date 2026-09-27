"""Real-data demo bootstrap: registry -> historical trades -> 1h candles -> signals.

This is intentionally a convenience service, not a second data path.  It uses the
same authoritative TSETMC trade collector, CandleService and ScannerService as
the rest of the toolkit so a dashboard can be populated outside market hours.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta

from ..candles.models import Timeframe
from ..candles.service import CandleService
from ..models import Instrument
from ..registry.sync import RegistrySyncService
from ..scanner.service import ScannerConfig, ScannerService
from ..services.history import TradeHistoryCollector
from ..timeutil import now_tehran


@dataclass
class DemoReport:
    symbol: str
    ins_code: str
    sessions_requested: int
    sessions_loaded: int = 0
    trades_loaded: int = 0
    candles_built: int = 0
    signals_generated: int = 0
    first_day: str | None = None
    last_day: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class DemoBootstrapService:
    def __init__(self, container) -> None:
        self.c = container

    async def run(
        self,
        *,
        symbol: str,
        ins_code: str | None = None,
        sessions: int = 5,
        timeframe: str = "1h",
        period: int = 10,
        multiplier: float = 3.0,
        sync_registry: bool = True,
        max_lookback_days: int = 30,
    ) -> DemoReport:
        if sessions < 1:
            raise ValueError("sessions must be >= 1")
        if sync_registry:
            await RegistrySyncService(
                self.c.webgw, self.c.legacy, self.c.instruments_repo
            ).sync()

        inst = self._resolve_instrument(symbol, ins_code)
        if not inst.ins_code:
            raise ValueError(
                f"{symbol!r} is in the registry but has no TSETMC InsCode mapping. "
                "Pass --ins-code explicitly (for فولاد: 46348559193224090)."
            )

        report = DemoReport(symbol=inst.symbol or symbol, ins_code=inst.ins_code,
                            sessions_requested=sessions)
        history = TradeHistoryCollector(
            self.c.tsetmc, self.c.http_tsetmc, self.c.instruments_repo,
            self.c.trades_repo, self.c.fetch_repo, archive=self.c.archive,
            concurrency=self.c.settings.http_concurrency, skip_already_done=False,
        )
        tf = Timeframe(timeframe)
        candle_service = CandleService(
            self.c.trades_repo, self.c.candles_repo, self.c.instruments_repo,
            self.c.calendar, events=self.c.events,
        )
        scanner = ScannerService(
            self.c.candles_repo, self.c.signals_repo,
            ScannerConfig(timeframe=timeframe, period=period, multiplier=multiplier),
            events=self.c.events,
        )

        # Find N actual non-empty trading sessions by walking backwards.  The
        # configured calendar skips normal closed weekdays; exchange holidays
        # simply return zero trades and are skipped as well.
        loaded_days: list[date] = []
        cursor = now_tehran().date()
        checked = 0
        while len(loaded_days) < sessions and checked < max_lookback_days:
            checked += 1
            if not self.c.calendar.is_trading_day(cursor):
                cursor -= timedelta(days=1)
                continue
            outcome = await history.fetch_day(inst.ins_code, cursor, instrument_id=inst.id)
            if outcome.status == "OK" and outcome.trades > 0:
                loaded_days.append(cursor)
                report.trades_loaded += outcome.trades
            cursor -= timedelta(days=1)

        if not loaded_days:
            raise RuntimeError(
                f"No non-empty TSETMC sessions found for {symbol!r} in the last "
                f"{max_lookback_days} calendar days."
            )

        # Build/replay oldest -> newest so indicator transitions are chronological.
        for day in sorted(loaded_days):
            trades = self.c.trades_repo.get_trades(inst.id, day)
            candles = candle_service.build_and_store(inst.id, day, tf, trades=trades)
            report.candles_built += len(candles)
            for candle in candles:
                if scanner.evaluate(candle) is not None:
                    report.signals_generated += 1

        loaded_days.sort()
        report.sessions_loaded = len(loaded_days)
        report.first_day = loaded_days[0].isoformat()
        report.last_day = loaded_days[-1].isoformat()
        return report

    def _resolve_instrument(self, symbol: str, ins_code: str | None) -> Instrument:
        if ins_code:
            by_code = self.c.instruments_repo.get_by_ins_code(ins_code)
            if by_code is not None:
                return by_code

        matches = self.c.instruments_repo.get_by_symbol(symbol)
        inst = next((x for x in matches if x.active and x.ins_code), None)
        if inst is None:
            inst = next((x for x in matches if x.active), None)
        if inst is None and matches:
            inst = matches[0]

        if inst is not None and ins_code and inst.ins_code != ins_code:
            inst = self.c.instruments_repo.upsert(inst.model_copy(update={"ins_code": ins_code}))
            return inst
        if inst is not None:
            return inst

        if not ins_code:
            raise ValueError(
                f"symbol {symbol!r} not found after registry sync and no --ins-code was provided"
            )
        return self.c.instruments_repo.upsert(
            Instrument(symbol=symbol, ins_code=ins_code,
                       metadata={"provider": "manual-demo-mapping"})
        )
