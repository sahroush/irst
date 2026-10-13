"""Retention pruning: age-based deletion + VACUUM, off by default."""
from __future__ import annotations

from datetime import datetime, timedelta

from iran_market.models import Candle, CandleStatus, Signal, Trade
from iran_market.storage.sqlite import (
    SqliteCandleRepository, SqliteFetchRunRepository, SqliteSignalRepository,
    SqliteStore, SqliteTradeRepository, create_schema, prune_before,
)
from iran_market.timeutil import TEHRAN, now_tehran

from .conftest import INS_ID

OLD = datetime(2026, 1, 1, 9, 0, tzinfo=TEHRAN)


def make_store(tmp_path):
    store = SqliteStore(tmp_path / "test.db")
    create_schema(store)
    return store


def make_candle(start: datetime) -> Candle:
    return Candle(
        instrument_id=INS_ID, timeframe="30m", start_time=start,
        end_time=start + timedelta(minutes=30),
        open=100, high=110, low=90, close=105, volume=1000, trade_count=5,
        status=CandleStatus.CLOSED, source="trades",
    )


def make_trade(day: datetime, seq: int = 1) -> Trade:
    return Trade(
        instrument_id=INS_ID, trade_date=day.date(), timestamp=day,
        sequence=seq, price=100, volume=10, source="tsetmc",
    )


def make_signal(candle_time: datetime) -> Signal:
    return Signal(
        id=f"s-{candle_time.isoformat()}", scanner_name="supertrend",
        instrument_id=INS_ID, timeframe="30m", candle_time=candle_time,
        signal_type="supertrend_flip", direction="bullish",
    )


def test_prune_deletes_old_rows_keeps_recent(tmp_path):
    store = make_store(tmp_path)
    recent = now_tehran().replace(microsecond=0)
    cutoff = recent - timedelta(days=10)
    candles = SqliteCandleRepository(store)
    candles.upsert_many([make_candle(OLD), make_candle(recent)])
    trades = SqliteTradeRepository(store)
    trades.upsert_many([make_trade(OLD), make_trade(recent)])
    signals = SqliteSignalRepository(store)
    signals.upsert(make_signal(OLD))
    signals.upsert(make_signal(recent))
    fr = SqliteFetchRunRepository(store)
    # Insert the old run via raw SQL: SqliteFetchRunRepository always stamps
    # started_at=now, so age can't be injected through the repo API.
    conn = store.connect()
    with conn:
        conn.execute(
            "INSERT INTO fetch_runs (provider, operation, instrument, requested_date,"
            " started_at, status) VALUES (?,?,?,?,?,?)",
            ("tsetmc", "get_trades", "X", OLD.date().isoformat(),
             OLD.replace(tzinfo=None).isoformat(), "OK"),
        )
    fr.finish(fr.start("tsetmc", "get_trades", "X", recent.date()), status="OK", duration_ms=1)

    report = prune_before(store, days=90)
    assert report == {"candles": 1, "trades": 1, "signals": 1, "fetch_runs": 1}
    assert len(candles.get_series(INS_ID, "30m")) == 1
    assert len(trades.get_trades(INS_ID, recent.date())) == 1
    assert trades.get_trades(INS_ID, OLD.date()) == []
    assert len(signals.list_recent(recent - timedelta(minutes=1))) == 1
    n = store.connect().execute("SELECT COUNT(*) AS n FROM fetch_runs").fetchone()["n"]
    assert n == 1
