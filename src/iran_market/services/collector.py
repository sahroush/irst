"""Live MarketWatchCollector: one bulk request, stats, optional watch filter."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from ..errors import ProviderError
from ..events import EventBus
from ..logging_setup import get_logger
from ..models import MarketSnapshot
from ..providers.base import MarketDataProvider
from ..timeutil import now_tehran

log = get_logger("collector")


@dataclass
class CollectorStats:
    """Extensible counters; Prometheus adapter can read these later."""

    requests: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    http_errors: int = 0
    parse_errors: int = 0
    instruments_returned: int = 0
    instruments_matched: int = 0
    last_duration_ms: int = 0
    last_success_at: datetime | None = None
    last_error: str | None = None

    def snapshot(self) -> dict:
        return dict(self.__dict__)


class MarketWatchCollector:
    """Polls a bulk market-watch provider and publishes normalized snapshots.

    One request covers the whole cash market, so hundreds of instruments are
    not hundreds of requests. Interval scheduling lives with the caller
    (CLI loop, cron, systemd timer); this class does a single poll + stats.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        watch_isins: set[str] | None = None,
        watch_symbols: set[str] | None = None,
        events: EventBus | None = None,
    ) -> None:
        self._provider = provider
        self._watch_isins = watch_isins or None
        self._watch_symbols = watch_symbols or None
        self._events = events
        self.stats = CollectorStats()

    async def poll(self) -> list[MarketSnapshot]:
        """One poll cycle. Raises on provider failure after stats update."""
        started = time.monotonic()
        self.stats.requests += 1
        try:
            snapshots = await self._provider.get_market_snapshot()
        except ProviderError as exc:
            self.stats.consecutive_failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            if "HTTP" in self.stats.last_error or "timeout" in self.stats.last_error.lower():
                self.stats.http_errors += 1
            else:
                self.stats.parse_errors += 1
            log.error("market watch poll failed", extra={"error": self.stats.last_error})
            raise
        except Exception as exc:  # malformed rows are skipped inside provider
            self.stats.consecutive_failures += 1
            self.stats.parse_errors += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            log.error("market watch poll crashed", extra={"error": self.stats.last_error})
            raise

        matched = self._filter(snapshots)
        duration_ms = int((time.monotonic() - started) * 1000)
        self.stats.successes += 1
        self.stats.consecutive_failures = 0
        self.stats.instruments_returned = len(snapshots)
        self.stats.instruments_matched = len(matched)
        self.stats.last_duration_ms = duration_ms
        self.stats.last_success_at = now_tehran()
        if self._events is not None:
            for s in matched:
                self._events.publish("MarketSnapshotReceived", {"snapshot": s})
        log.info("market watch poll ok",
                 extra={"returned": len(snapshots), "matched": len(matched), "ms": duration_ms})
        return matched

    def _filter(self, snapshots: list[MarketSnapshot]) -> list[MarketSnapshot]:
        if not self._watch_isins and not self._watch_symbols:
            return snapshots
        out = []
        for s in snapshots:
            if self._watch_isins and s.isin in self._watch_isins:
                out.append(s)
            elif self._watch_symbols and s.symbol in self._watch_symbols:
                out.append(s)
        return out
