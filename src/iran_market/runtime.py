"""Single-process runtime: catch-up, live scanner, dashboard, auto-reconcile.

`serve` is the "one service, everything" entrypoint:
  1. On startup: registry sync + incremental history catch-up (resumable via
     fetch_runs; reuses PrefetchService so prefetch/backfill semantics hold).
  2. During market hours: scanner loop (WebGW bulk poll -> live candles -> scanner).
  3. Uvicorn dashboard runs concurrently for the whole lifetime.
  4. After each session ends: auto-reconcile that day's candles from authoritative
     TSETMC trade history, replaying Supertrend on the corrected (RECONCILED) bars.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from .candles.models import Timeframe
from .candles.service import CandleService
from .logging_setup import get_logger
from .registry.sync import RegistrySyncService
from .scanner.service import ScannerConfig, ScannerService
from .services.history import TradeHistoryCollector
from .services.prefetch import PrefetchService
from .storage.sqlite import prune_before
from .timeutil import now_tehran

log = get_logger("runtime")


class ReconciliationService:
    """Post-session exactness: replace provisional candles with trade-derived ones.

    For a given day: fetch TSETMC trade history for every TSETMC-mapped active
    instrument that already has any candle that day (the watch universe is the
    universe the live path produced), then rebuild candles via
    CandleService.reconcile() -> status RECONCILED, and replay the scanner over
    the corrected bars. Idempotent end to end (UPSERT + idempotent signals).
    """

    def __init__(self, container) -> None:
        self.c = container

    async def run_day(self, day, *, timeframe: str, period: int = 10,
                      multiplier: float = 3.0) -> dict:
        tf = Timeframe(timeframe)
        instruments = [i for i in self.c.instruments_repo.list_active() if i.ins_code]
        if not instruments:
            return {"day": day.isoformat(), "instruments": 0, "fetched": 0,
                    "candles_reconciled": 0, "signals": 0}

        # Reconcile only instruments that participated in the live day: having a
        # candle for `day` (any timeframe) means the WebGW path observed it.
        candle_repo = self.c.candles_repo
        win = self.c.calendar.day_window(day)
        day_end = win.end if win else now_tehran()
        targets = []
        for inst in instruments:
            series = candle_repo.get_series(inst.id, tf.code,
                                            start=win.start if win else None,
                                            end=day_end + timedelta(seconds=1))
            if series:
                targets.append(inst)
        if not targets:
            return {"day": day.isoformat(), "instruments": 0, "fetched": 0,
                    "candles_reconciled": 0, "signals": 0}

        history = TradeHistoryCollector(
            self.c.tsetmc, self.c.http_tsetmc, self.c.instruments_repo,
            self.c.trades_repo, self.c.fetch_repo, archive=self.c.archive,
            concurrency=self.c.settings.http_concurrency, skip_already_done=False,
        )
        requests = [(inst.ins_code, day) for inst in targets]
        outcomes = await history.fetch_many(requests)

        candle_service = CandleService(
            self.c.trades_repo, self.c.candles_repo, self.c.instruments_repo,
            self.c.calendar, events=self.c.events,
        )
        scanner = ScannerService(
            self.c.candles_repo, self.c.signals_repo,
            ScannerConfig(timeframe=timeframe, period=period, multiplier=multiplier),
            events=self.c.events,
        )

        reconciled = 0
        signals = 0
        for inst, outcome in zip(targets, outcomes):
            if outcome.status == "FAILED":
                continue
            trades = self.c.trades_repo.get_trades(inst.id, day)
            if not trades:
                continue
            candles = candle_service.reconcile(inst.id, day, tf, trades)
            reconciled += len(candles)
            for candle in candles:
                if scanner.evaluate(candle) is not None:
                    signals += 1

        report = {"day": day.isoformat(), "instruments": len(targets),
                  "fetched": sum(o.status == "OK" for o in outcomes),
                  "candles_reconciled": reconciled, "signals": signals}
        log.info("reconciliation complete", extra=report)
        return report


class MarketRuntime:
    """Wires catch-up, scanner loop, and after-session reconciliation together."""

    def __init__(self, container, *, timeframe: str, period: int = 10,
                 multiplier: float = 3.0, catch_up_sessions: int = 30) -> None:
        self.c = container
        self.tf = Timeframe(timeframe)
        self.period = period
        self.multiplier = multiplier
        self.catch_up_sessions = catch_up_sessions
        self.scanner_cfg = ScannerConfig(
            timeframe=timeframe, period=period, multiplier=multiplier
        )
        self.reconciler = ReconciliationService(container)
        self._reconciled_days: set = set()
        self._reconcile_task: asyncio.Future | None = None
        self._pruned_days: set = set()

    async def catch_up(self, *, sync_registry: bool = True) -> dict:
        """Startup historical bootstrap: registry sync + resumable prefetch."""
        if sync_registry:
            await RegistrySyncService(self.c.webgw, self.c.legacy, self.c.instruments_repo).sync()
        svc = PrefetchService(self.c)
        report = await svc.run(
            sessions=self.catch_up_sessions,
            timeframes=[self.tf.code],
            signal_timeframe=self.tf.code,
            period=self.period,
            multiplier=self.multiplier,
            sync_registry=False,  # already synced above
        )
        log.info("catch-up complete", extra={"candles": report.candles_built,
                                             "signals": report.signals_generated})
        return report.to_dict()

    async def run_forever(self, stop_event: asyncio.Event, interval: float) -> None:
        """Scanner loop + session-end reconciliation until `stop_event`."""
        from .scanner.runner import LiveScannerRunner

        runner = LiveScannerRunner(self.c, self.scanner_cfg)
        while not stop_event.is_set():
            now = now_tehran()
            win = self.c.calendar.primary_session(now.date())
            try:
                if win is not None and win.start <= now < win.end:
                    await runner.once()
                    await self._wait(stop_event, interval)
                else:
                    # Refresh observation state before potentially slow reconciliation.
                    await runner.once()
                    # Market closed: finalize + reconcile the just-finished session.
                    self._schedule_reconcile(stop_event, now.date())
                    self._maybe_prune(now.date())
                    await self._wait(stop_event, max(interval, 30.0))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("runtime loop error", extra={"error": f"{type(exc).__name__}: {exc}"})
                await self._wait(stop_event, max(interval, 30.0))

    def _maybe_prune(self, day) -> None:
        """Once per day: delete trades/fetch_runs older than retention_days, then VACUUM.

        Candles and signals are never pruned (see storage.sqlite.prune_before).

        Runs inline (market is closed, no polling pressure) but is skipped when
        retention_days=0. VACUUM can take minutes on a large trades table; the
        market-closed branch polls at 30s so a slow prune only delays the next
        closed-market poll, not live candle building.
        """
        days = self.c.settings.retention_days
        if days <= 0 or day in self._pruned_days:
            return
        self._pruned_days.add(day)
        try:
            report = prune_before(self.c.store, days)
            if any(report.values()):
                log.info("retention prune", extra=report)
        except Exception as exc:
            self._pruned_days.discard(day)  # retry next closed-market pass
            log.error("retention prune failed", extra={"error": f"{type(exc).__name__}: {exc}"})

    def _schedule_reconcile(self, stop_event: asyncio.Event, day) -> None:
        """Run day reconciliation alongside polling, never blocking it.

        Reconciliation fetches trade history for the whole universe and takes
        minutes. Awaiting it inline starved `live_snapshot_state` refreshes and
        tripped the dashboard's feed-health freshness check to DEGRADED even
        though polling was healthy.
        """
        if day in self._reconciled_days or self._reconcile_task is not None:
            return
        self._reconcile_task = asyncio.ensure_future(self._reconcile_finished_day(day))
        self._reconcile_task.add_done_callback(self._on_reconcile_done)

    def _on_reconcile_done(self, task: asyncio.Future) -> None:
        self._reconcile_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("reconciliation task failed", extra={"error": f"{type(exc).__name__}: {exc}"})

    async def run_with_catch_up(self, stop_event: asyncio.Event, interval: float) -> None:
        """Run live polling immediately; bootstrap historical data in background."""
        catchup_task = asyncio.create_task(self._catch_up_until_complete(stop_event))
        try:
            await self.run_forever(stop_event, interval)
        finally:
            catchup_task.cancel()
            try:
                await catchup_task
            except asyncio.CancelledError:
                pass

    async def _catch_up_until_complete(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await RegistrySyncService(self.c.webgw, self.c.legacy, self.c.instruments_repo).sync()
                report = await self.catch_up(sync_registry=False)
                if not report.get("failed"):
                    return
                log.warning("catch-up incomplete; retrying failed instrument-days",
                            extra={"failed": report["failed"]})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("startup sync/catch-up failed; live scanner remains active",
                          extra={"error": f"{type(exc).__name__}: {exc}"})
            await self._wait(stop_event, 300.0)

    async def _reconcile_finished_day(self, day) -> None:
        """Reconcile the finished session once per process per day (idempotent)."""
        if day in self._reconciled_days:
            return
        # Only after the session actually ended (or on startup for an earlier day).
        now = now_tehran()
        win = self.c.calendar.day_window(day)
        if win is not None and now < win.end:
            return
        self._reconciled_days.add(day)
        try:
            report = await self.reconciler.run_day(
                day, timeframe=self.tf.code, period=self.period,
                multiplier=self.multiplier,
            )
            log.info("post-session reconciliation", extra=report)
        except Exception as exc:
            self._reconciled_days.discard(day)  # allow retry on next loop pass
            log.error("reconciliation failed", extra={"error": f"{type(exc).__name__}: {exc}"})

    @staticmethod
    async def _wait(stop_event: asyncio.Event, timeout: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
