"""Health/metrics usable by CLI and dashboard without an external monitoring stack."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from ..timeutil import now_tehran


@dataclass
class Metrics:
    polls_total: int = 0
    polls_failed: int = 0
    trades_fetched_total: int = 0
    candles_built_total: int = 0
    provider_errors_total: int = 0
    last_snapshot_at: float = field(default_factory=time.monotonic)
    def snapshot(self): return dict(self.__dict__)


class Health:
    def __init__(self) -> None:
        self.metrics = Metrics(); self._components = {}
    def register(self, name, stats_provider): self._components[name] = stats_provider
    def report(self):
        out = {"metrics": self.metrics.snapshot()}
        for name, provider in self._components.items():
            try: out[name] = provider() if callable(provider) else provider
            except Exception as exc: out[name] = {"error": str(exc)}
        return out


class HealthService:
    """Database-derived health view; safe across process restarts."""
    def __init__(self, container): self.c = container

    def report(self) -> dict:
        conn = self.c.store.connect(); now = now_tehran()
        active = conn.execute("SELECT COUNT(*) n FROM instruments WHERE active=1").fetchone()["n"]
        building = conn.execute("SELECT COUNT(*) n FROM candles WHERE status='BUILDING'").fetchone()["n"]
        signals = conn.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"]
        last_state = conn.execute("SELECT MAX(ts) t FROM live_snapshot_state").fetchone()["t"]
        last_closed = conn.execute("SELECT MAX(end_time) t FROM candles WHERE status IN ('CLOSED','RECONCILED')").fetchone()["t"]
        last_fetch = conn.execute("SELECT MAX(finished_at) t FROM fetch_runs WHERE status='OK'").fetchone()["t"]
        stale_seconds = None
        if last_state:
            ts = datetime.fromisoformat(last_state)
            stale_seconds = max(0, int((now - ts).total_seconds()))
        win = self.c.calendar.primary_session(now.date())
        market_state = "OPEN" if win is not None and win.start <= now < win.end else "CLOSED"
        freshness_limit = max(120, self.c.settings.market_watch_interval_seconds * 2)
        if not active:
            status = "UNHEALTHY"
        else:
            status = "HEALTHY" if stale_seconds is not None and stale_seconds <= freshness_limit else "DEGRADED"
        return {
            "status": status, "market_state": market_state, "now": now.isoformat(), "active_instruments": active,
            "building_candles": building, "signals": signals,
            "last_snapshot_time": last_state, "snapshot_age_seconds": stale_seconds,
            "last_closed_candle": last_closed, "last_successful_fetch": last_fetch,
        }
