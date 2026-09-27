"""30m session-anchored buckets, live close, reconcile-after-close, serve smoke."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from iran_market.candles.models import Timeframe
from iran_market.candles.service import CandleService
from iran_market.config import Settings
from iran_market.events import EventBus
from iran_market.models import Candle, CandleStatus, Instrument, MarketSnapshot, Trade
from iran_market.runtime import ReconciliationService
from iran_market.services.live_candles import LiveCandleService
from iran_market.sessions import TradingCalendar
from iran_market.storage.sqlite import (SqliteCandleRepository, SqliteFetchRunRepository,
    SqliteInstrumentRepository, SqliteLiveStateRepository, SqliteSignalRepository,
    SqliteStore, SqliteTradeRepository, create_schema)
from iran_market.timeutil import TEHRAN

IID = UUID("22222222-2222-2222-2222-222222222222")
DAY = date(2026, 9, 23)  # Wednesday, open in default calendar

TF30 = Timeframe("30m")


def test_thirty_minute_edges_session_anchored():
    edges = TradingCalendar().minute_boundaries(DAY, 30)
    starts = [s.strftime("%H:%M") for s, _ in edges]
    assert starts == ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00"]
    last_start, last_end = edges[-1]
    assert (last_start.hour, last_start.minute) == (12, 0)
    assert (last_end.hour, last_end.minute) == (12, 30)  # absorbs remainder


def test_thirty_minute_bucket_mapping():
    edges = TradingCalendar().minute_boundaries(DAY, 30)
    def bucket(hm):
        ts = datetime(2026, 9, 23, *hm, tzinfo=TEHRAN)
        for s, e in edges:
            if s <= ts < e:
                return s.strftime("%H:%M")
        return None
    assert bucket((9, 0)) == "09:00"
    assert bucket((9, 29)) == "09:00"
    assert bucket((9, 30)) == "09:30"
    assert bucket((12, 15)) == "12:00"
    assert bucket((12, 30)) is None  # session over
    assert bucket((8, 59)) is None   # pre-open


def test_live_candle_30m_delta_close(tmp_path):
    store = SqliteStore(tmp_path / "db.sqlite"); create_schema(store)
    candles = SqliteCandleRepository(store); states = SqliteLiveStateRepository(store)
    svc = LiveCandleService(candles, states, TradingCalendar(), EventBus())
    t = datetime(2026, 9, 23, 9, 5, tzinfo=TEHRAN)
    s1 = MarketSnapshot(instrument_id=IID, isin="X", timestamp=t, last_price=100,
                        cumulative_volume=1000, trade_count=10)
    s2 = MarketSnapshot(instrument_id=IID, isin="X", timestamp=t + timedelta(minutes=20),
                        last_price=105, cumulative_volume=1500, trade_count=14)
    c = svc.process(s1, TF30); c2nd = svc.process(s2, TF30)
    assert c2nd is not None
    c = c2nd
    assert (c.open, c.high, c.low, c.close, c.volume, c.trade_count) == (100, 105, 100, 105, 500, 4)
    assert c.status == CandleStatus.BUILDING and c.start_time.hour == 9 and c.start_time.minute == 0
    closed = svc.close_elapsed(datetime(2026, 9, 23, 9, 31, tzinfo=TEHRAN), TF30)
    assert len(closed) == 1 and closed[0].status == CandleStatus.CLOSED
    # Second bucket starts fresh; first snapshot of new bucket is baseline.
    s3 = MarketSnapshot(instrument_id=IID, isin="X",
                        timestamp=datetime(2026, 9, 23, 9, 35, tzinfo=TEHRAN),
                        last_price=106, cumulative_volume=1600, trade_count=15)
    c2 = svc.process(s3, TF30)
    assert c2 is not None and c2.start_time.minute == 30 and c2.volume == 100 and c2.trade_count == 1


class FakeTsetmcProvider:
    name = "tsetmc"

    def __init__(self, trades_by_day):
        self._trades = trades_by_day

    async def get_trades(self, ins_code, day):
        return list(self._trades.get((ins_code, day), []))


def _trade(inst_id, day, seq, hour, minute, price, volume=100):
    return Trade(instrument_id=inst_id, trade_date=day,
                 timestamp=datetime(day.year, day.month, day.day, hour, minute, tzinfo=TEHRAN),
                 sequence=seq, price=price, volume=volume, source="tsetmc")


def test_reconcile_after_close_marks_reconciled(tmp_path):
    store = SqliteStore(tmp_path / "db.sqlite"); create_schema(store)
    inst = Instrument(id=IID, symbol="FOLAD", ins_code="463", active=True)
    instruments = SqliteInstrumentRepository(store); instruments.upsert(inst)
    trades_repo = SqliteTradeRepository(store)
    candles = SqliteCandleRepository(store); signals = SqliteSignalRepository(store)
    fetch_repo = SqliteFetchRunRepository(store)

    # Provisional live candle for the first 30m bucket (as WebGW would leave it).
    provisional = Candle(instrument_id=IID, timeframe="30m",
                         start_time=datetime(2026, 9, 23, 9, 0, tzinfo=TEHRAN),
                         end_time=datetime(2026, 9, 23, 9, 30, tzinfo=TEHRAN),
                         open=100, high=102, low=99, close=101, volume=400,
                         trade_count=4, status=CandleStatus.CLOSED, source="tse_webgw")
    candles.upsert(provisional)

    # Authoritative trades: slightly different OHLCV than the snapshot path saw.
    real = [_trade(IID, DAY, 1, 9, 2, 100), _trade(IID, DAY, 2, 9, 10, 105),
            _trade(IID, DAY, 3, 9, 20, 98, volume=200)]
    provider = FakeTsetmcProvider({("463", DAY): real})

    cal = TradingCalendar()
    c = SimpleNamespace(
        tsetmc=provider, http_tsetmc=None, instruments_repo=instruments,
        trades_repo=trades_repo, candles_repo=candles, signals_repo=signals,
        fetch_repo=fetch_repo, calendar=cal, events=EventBus(), archive=None,
        settings=Settings.for_tests(),
    )
    svc = ReconciliationService(c)
    report = asyncio.run(svc.run_day(DAY, timeframe="30m", period=3, multiplier=1.5))
    assert report["instruments"] == 1 and report["fetched"] == 1
    assert report["candles_reconciled"] == 1

    row = candles.get(IID, "30m", datetime(2026, 9, 23, 9, 0, tzinfo=TEHRAN))
    assert row is not None and row.status == CandleStatus.RECONCILED
    assert (row.open, row.high, row.low, row.close) == (100, 105, 98, 98)
    assert row.volume == 400 and row.trade_count == 3
    assert row.reconciled_at is not None
    # Idempotent: rerun replaces, never duplicates.
    report2 = asyncio.run(svc.run_day(DAY, timeframe="30m", period=3, multiplier=1.5))
    assert report2["candles_reconciled"] == 1
    assert len(candles.get_series(IID, "30m")) == 1
