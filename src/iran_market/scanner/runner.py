"""Single-process live scanner orchestration."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..candles.models import Timeframe
from ..events import EventBus
from ..registry.sync import RegistrySyncService
from ..services.collector import MarketWatchCollector
from ..services.live_candles import LiveCandleService
from ..timeutil import now_tehran
from .service import ScannerConfig, ScannerService


@dataclass
class ScannerRunStats:
    polls: int = 0
    snapshots: int = 0
    resolved: int = 0
    candles_closed: int = 0
    signals: int = 0


class LiveScannerRunner:
    def __init__(self, container, config: ScannerConfig | None = None):
        self.c = container
        self.config = config or ScannerConfig()
        self.tf = Timeframe(self.config.timeframe)
        self.events = container.events
        self.collector = MarketWatchCollector(container.webgw, events=self.events)
        self.live = LiveCandleService(container.candles_repo, container.live_state_repo,
                                      container.calendar, events=self.events)
        self.scanner = ScannerService(container.candles_repo, container.signals_repo, self.config, events=self.events)
        self.stats = ScannerRunStats()

    async def sync_registry(self):
        return await RegistrySyncService(self.c.webgw, self.c.legacy, self.c.instruments_repo).sync()

    async def once(self) -> ScannerRunStats:
        snaps = await self.collector.poll()
        self.stats.polls += 1
        self.stats.snapshots += len(snaps)
        for s in snaps:
            inst = self.c.instruments_repo.get_by_isin(s.isin) if s.isin else None
            if inst is None:
                continue
            s.instrument_id = inst.id
            self.stats.resolved += 1
            self.live.process(s, self.tf)
        closed = self.live.close_elapsed(now_tehran(), self.tf)
        self.stats.candles_closed += len(closed)
        for candle in closed:
            if self.scanner.evaluate(candle):
                self.stats.signals += 1
        return self.stats

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        await self.sync_registry()
        interval = self.c.settings.market_watch_interval_seconds
        while not stop_event.is_set():
            now = now_tehran()
            win = self.c.calendar.primary_session(now.date())
            if win is None or now < win.start or now >= win.end:
                closed = self.live.close_session_candles(now.date(), self.tf)
                self.stats.candles_closed += len(closed)
                for candle in closed:
                    if self.scanner.evaluate(candle):
                        self.stats.signals += 1
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=max(interval, 30.0))
                except asyncio.TimeoutError:
                    pass
                continue
            await self.once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
