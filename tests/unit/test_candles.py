"""CandleBuilder semantics: OHLC from chronology, dup handling, session edges."""

from datetime import date, datetime, timedelta
from uuid import UUID

from iran_market.candles.builder import CandleBuilder, bucket_key
from iran_market.candles.models import Timeframe
from iran_market.models import Trade
from iran_market.sessions import SessionConfig, TradingCalendar
from iran_market.timeutil import TEHRAN

from .conftest import INS_ID

DAY = date(2026, 9, 23)


def trade(h: int, m: int, s: int, price: int, seq: int, volume: int = 100, canceled: bool = False) -> Trade:
    return Trade(
        instrument_id=INS_ID, trade_date=DAY,
        timestamp=datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=TEHRAN),
        sequence=seq, price=price, volume=volume, canceled=canceled, source="test",
    )


def hour_edges(d: date) -> list[tuple[datetime, datetime]]:
    cal = TradingCalendar(SessionConfig())  # 09:00-12:30
    return cal.hour_boundaries(d, 1)


def test_bucket_key_containment():
    edges = hour_edges(DAY)
    assert bucket_key(datetime(2026, 9, 23, 9, 0, 0, tzinfo=TEHRAN), edges) == 0
    assert bucket_key(datetime(2026, 9, 23, 9, 59, 59, tzinfo=TEHRAN), edges) == 0
    assert bucket_key(datetime(2026, 9, 23, 10, 0, 0, tzinfo=TEHRAN), edges) == 1
    assert bucket_key(datetime(2026, 9, 23, 12, 15, 0, tzinfo=TEHRAN), edges) == 3
    assert bucket_key(datetime(2026, 9, 23, 8, 59, 59, tzinfo=TEHRAN), edges) is None
    assert bucket_key(datetime(2026, 9, 23, 12, 31, 0, tzinfo=TEHRAN), edges) is None


def test_session_hour_edges_shape():
    edges = hour_edges(DAY)
    assert [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in edges] == [
        ("09:00", "10:00"), ("10:00", "11:00"), ("11:00", "12:00"), ("12:00", "12:30"),
    ]


def test_ohlc_first_open_last_close():
    """open/close from chronology, NOT from input order."""
    trades = [
        trade(9, 55, 5, 3390, seq=900),   # chronologically last (close)
        trade(9, 5, 0, 3300, seq=100),    # chronologically first (open)
        trade(9, 30, 0, 3400, seq=200),   # high
        trade(9, 15, 0, 3200, seq=150),   # low
    ]
    builder = CandleBuilder()
    candles = builder.build(trades, hour_edges(DAY), Timeframe("1h"))
    assert len(candles) == 1
    c = candles[0]
    assert (c.open, c.high, c.low, c.close) == (3300, 3400, 3200, 3390)
    assert c.volume == 100 * 4 and c.trade_count == 4


def test_volume_aggregation_and_trade_count():
    trades = [trade(9, 0, i, 3300 + i, seq=i, volume=10 * i) for i in range(1, 6)]
    candles = CandleBuilder().build(trades, hour_edges(DAY), Timeframe("1h"))
    assert candles[0].volume == 10 * (1 + 2 + 3 + 4 + 5)
    assert candles[0].trade_count == 5


def test_duplicate_sequences_dedup_idempotent():
    t1 = trade(9, 0, 0, 3300, seq=42)
    t1b = trade(9, 0, 0, 3300, seq=42)  # same identity, replayed
    t2 = trade(9, 5, 0, 3310, seq=43)
    trades = CandleBuilder.dedupe([t1, t1b, t2])
    assert len(trades) == 2
    # idempotent: dedupe of dedupe = same
    assert len(CandleBuilder.dedupe(trades)) == 2


def test_canceled_never_in_candles():
    trades = [trade(9, 0, 0, 99000, seq=1, canceled=True), trade(9, 1, 0, 3300, seq=2)]
    candles = CandleBuilder().build(trades, hour_edges(DAY), Timeframe("1h"))
    assert candles[0].high == 3300 and candles[0].trade_count == 1


def test_empty_bucket_no_candle_unless_configured():
    trades = [trade(9, 5, 0, 3300, seq=1)]  # only first hour has trades
    builder = CandleBuilder()
    assert len(builder.build(trades, hour_edges(DAY), Timeframe("1h"))) == 1
    with_empty = builder.build(trades, hour_edges(DAY), Timeframe("1h"), include_empty=True)
    assert len(with_empty) == 4


def test_out_of_session_trades_dropped():
    trades = [trade(8, 0, 0, 3300, seq=1), trade(13, 0, 0, 3400, seq=2),
              trade(9, 30, 0, 3350, seq=3)]
    candles = CandleBuilder().build(trades, hour_edges(DAY), Timeframe("1h"))
    assert len(candles) == 1 and candles[0].close == 3350


def test_timeframe_parsing():
    for code, seconds in [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("1d", 86400)]:
        assert Timeframe(code).timedelta.total_seconds() == seconds
    import pytest
    with pytest.raises(ValueError):
        Timeframe("7x")
