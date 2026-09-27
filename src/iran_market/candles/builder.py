"""Generic CandleBuilder. Knows nothing about TSETMC.

Input: normalized Trades + session-anchored bucket edges from
:class:`~iran_market.sessions.TradingCalendar`. Output: Candles.

Semantics (verified by tests):
- open = first trade chronologically, close = last trade chronologically
  (never derived from API response order)
- high/low = max/min price over the bucket
- volume = sum, trade_count = count
- canceled trades never reach here (provider filters); if one appears it is
  counted out defensively
- duplicate (instrument, date, sequence) rows dedup idempotently
- empty buckets produce no candle unless include_empty=True
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from ..logging_setup import get_logger
from ..models import Candle, CandleStatus, Trade
from .models import Timeframe

log = get_logger("candles")


def bucket_key(ts: datetime, edges: Sequence[tuple[datetime, datetime]]) -> int | None:
    """Index of the [start, end) bucket containing ts, else None."""
    lo, hi = 0, len(edges) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end = edges[mid]
        if ts < start:
            hi = mid - 1
        elif ts >= end:
            lo = mid + 1
        else:
            return mid
    return None


class CandleBuilder:
    """Aggregates trades into candles over session-anchored bucket edges."""

    def build(
        self,
        trades: Iterable[Trade],
        edges: Sequence[tuple[datetime, datetime]],
        timeframe: Timeframe,
        *,
        include_empty: bool = False,
        source: str = "trades",
        status: CandleStatus = CandleStatus.CLOSED,
    ) -> list[Candle]:
        """Build candles. `trades` need not be sorted; we sort deterministically.

        Bucket edges come from the TradingCalendar (session-anchored). Trades
        outside all edges are ignored (logged at debug).
        """
        trades = [t for t in trades if not t.canceled]
        trades.sort(key=lambda t: (t.timestamp, t.sequence))

        buckets: dict[int, list[Trade]] = {}
        dropped = 0
        for t in trades:
            idx = bucket_key(t.timestamp, edges)
            if idx is None:
                dropped += 1
                continue
            buckets.setdefault(idx, []).append(t)
        if dropped:
            log.debug("trades outside session edges dropped", extra={"dropped": dropped})

        candles: list[Candle] = []
        for idx, (start, end) in enumerate(edges):
            group = buckets.get(idx)
            if not group:
                if include_empty:
                    candles.append(self._empty_candle(start, end, timeframe, source, status))
                continue
            first, last = group[0], group[-1]
            candles.append(
                Candle(
                    instrument_id=first.instrument_id,
                    timeframe=timeframe.code,
                    start_time=start,
                    end_time=end,
                    open=first.price,
                    high=max(t.price for t in group),
                    low=min(t.price for t in group),
                    close=last.price,
                    volume=sum(t.volume for t in group),
                    trade_count=len(group),
                    status=status,
                    source=source,
                )
            )
        return candles

    @staticmethod
    def _empty_candle(start, end, timeframe, source, status) -> Candle:
        return Candle(
            instrument_id=_NULL_ID,
            timeframe=timeframe.code,
            start_time=start,
            end_time=end,
            open=0, high=0, low=0, close=0,
            volume=0, trade_count=0,
            status=status, source=source,
        )

    @staticmethod
    def dedupe(trades: Iterable[Trade]) -> list[Trade]:
        """Idempotent dedup on (instrument, date, sequence), preserving first."""
        seen: set[tuple] = set()
        out: list[Trade] = []
        for t in trades:
            if t.key in seen:
                continue
            seen.add(t.key)
            out.append(t)
        return out


from uuid import UUID

_NULL_ID = UUID(int=0)
