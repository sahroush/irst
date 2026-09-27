from datetime import date, datetime, timedelta
from uuid import UUID

from iran_market.candles.models import Timeframe
from iran_market.config import Settings
from iran_market.events import EventBus
from iran_market.indicators.supertrend import SupertrendIndicator
from iran_market.models import Candle, CandleStatus, Instrument, MarketSnapshot
from iran_market.services.live_candles import LiveCandleService
from iran_market.sessions import TradingCalendar
from iran_market.storage.sqlite import (SqliteCandleRepository, SqliteLiveStateRepository,
    SqliteSignalRepository, SqliteStore, create_schema)
from iran_market.scanner.service import ScannerConfig, ScannerService
from iran_market.timeutil import TEHRAN

IID = UUID("11111111-1111-1111-1111-111111111111")


def candle(i, close, status=CandleStatus.CLOSED):
    start = datetime(2026, 9, 23, 9, 0, tzinfo=TEHRAN) + timedelta(hours=i)
    return Candle(instrument_id=IID, timeframe="1h", start_time=start, end_time=start+timedelta(hours=1),
                  open=close, high=close+5, low=close-5, close=close, volume=100, trade_count=1, status=status)


def test_supertrend_warmup_and_building_ignored():
    ind=SupertrendIndicator(period=3,multiplier=2)
    cs=[candle(i,100+i) for i in range(7)]
    cs.append(candle(7,50,CandleStatus.BUILDING))
    r=ind.calculate(cs)
    assert r.valid
    assert r.candle_time == cs[6].start_time
    assert r.state in {"BULLISH","BEARISH"}
    assert r.values["atr"] is not None


def test_live_candle_deltas_and_close_once(tmp_path):
    store=SqliteStore(tmp_path/"db.sqlite"); create_schema(store)
    candles=SqliteCandleRepository(store); states=SqliteLiveStateRepository(store); bus=EventBus(); seen=[]
    bus.subscribe("CandleClosed", lambda e,p: seen.append(p["candle"]))
    svc=LiveCandleService(candles,states,TradingCalendar(),bus)
    t=datetime(2026,9,23,9,10,tzinfo=TEHRAN)
    s1=MarketSnapshot(instrument_id=IID,isin="X",timestamp=t,last_price=100,cumulative_volume=1000,trade_count=10)
    s2=MarketSnapshot(instrument_id=IID,isin="X",timestamp=t+timedelta(minutes=5),last_price=110,cumulative_volume=1250,trade_count=13)
    c=svc.process(s1,Timeframe("1h")); c=svc.process(s2,Timeframe("1h"))
    assert (c.open,c.high,c.low,c.close,c.volume,c.trade_count)==(100,110,100,110,250,3)
    closed=svc.close_elapsed(datetime(2026,9,23,10,1,tzinfo=TEHRAN),Timeframe("1h"))
    assert len(closed)==1 and closed[0].status==CandleStatus.CLOSED and len(seen)==1
    assert svc.close_elapsed(datetime(2026,9,23,10,2,tzinfo=TEHRAN),Timeframe("1h"))==[]


def test_scanner_signal_is_idempotent(tmp_path):
    store=SqliteStore(tmp_path/"db.sqlite"); create_schema(store)
    candles=SqliteCandleRepository(store); signals=SqliteSignalRepository(store)
    # Rising history followed by a severe drop reliably flips a short-period Supertrend.
    closes=[100,100,100,105,110,115,120,125,130,135,80]
    for i,v in enumerate(closes): candles.upsert(candle(i,v))
    svc=ScannerService(candles,signals,ScannerConfig(timeframe="1h",period=3,multiplier=1.5,history=50))
    sig=svc.evaluate(candle(len(closes)-1,closes[-1]))
    assert sig is not None and sig.direction in {"bullish","bearish"}
    assert svc.evaluate(candle(len(closes)-1,closes[-1])) is None
    assert len(signals.get_latest())==1


def test_scanner_historical_replay_does_not_look_into_future(tmp_path):
    store=SqliteStore(tmp_path/"historical.sqlite"); create_schema(store)
    candles=SqliteCandleRepository(store); signals=SqliteSignalRepository(store)
    closes=[100,100,100,105,110,115,120,125,130,135,80]
    built=[]
    for i,v in enumerate(closes):
        c=candle(i,v); candles.upsert(c); built.append(c)
    svc=ScannerService(candles,signals,ScannerConfig(timeframe="1h",period=3,multiplier=1.5,history=50))
    # The severe drop at the final candle flips the latest trend. Evaluating an
    # earlier candle must not see that future flip or create a mis-dated signal.
    assert svc.evaluate(built[5]) is None
    assert signals.get_latest() == []
