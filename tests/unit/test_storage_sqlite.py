"""SQLite repos: UPSERT idempotency, uniqueness, fetch bookkeeping."""

from datetime import date, datetime
from uuid import uuid4

from iran_market.models import Candle, CandleStatus, Instrument, Trade
from iran_market.storage.sqlite import (
    SqliteCandleRepository,
    SqliteFetchRunRepository,
    SqliteInstrumentRepository,
    SqliteStore,
    SqliteTradeRepository,
    create_schema,
)
from iran_market.timeutil import TEHRAN, now_tehran

from .conftest import INS_ID


def make_store(tmp_path):
    store = SqliteStore(tmp_path / "test.db")
    create_schema(store)
    return store


def make_trade(seq: int, price: int = 3350, day: date = date(2026, 9, 23)) -> Trade:
    return Trade(
        instrument_id=INS_ID, trade_date=day,
        timestamp=datetime(day.year, day.month, day.day, 9, 0, seq % 60, tzinfo=TEHRAN),
        sequence=seq, price=price, volume=100, source="tsetmc",
    )


def test_trade_upsert_idempotent(tmp_path):
    repo = SqliteTradeRepository(make_store(tmp_path))
    trades = [make_trade(1), make_trade(2), make_trade(3)]
    assert repo.upsert_many(trades) == 3
    # re-run same batch: no duplicates
    repo.upsert_many(trades)
    assert repo.count_trades(INS_ID, date(2026, 9, 23)) == 3


def test_trade_conflicting_update_upserts(tmp_path):
    repo = SqliteTradeRepository(make_store(tmp_path))
    repo.upsert_many([make_trade(1, price=3350)])
    repo.upsert_many([make_trade(1, price=3400)])  # corrected upstream value
    got = repo.get_trades(INS_ID, date(2026, 9, 23))
    assert len(got) == 1 and got[0].price == 3400


def test_candle_upsert_idempotent_unique(tmp_path):
    repo = SqliteCandleRepository(make_store(tmp_path))
    start = datetime(2026, 9, 23, 9, 0, tzinfo=TEHRAN)
    end = datetime(2026, 9, 23, 10, 0, tzinfo=TEHRAN)

    def candle(price_close=3350):
        return Candle(
            instrument_id=INS_ID, timeframe="1h", start_time=start, end_time=end,
            open=3300, high=3400, low=3200, close=price_close, volume=12345,
            trade_count=42, status=CandleStatus.CLOSED, source="trades",
        )

    repo.upsert(candle())
    repo.upsert(candle(price_close=3390))  # rerun with slightly different data
    series = repo.get_series(INS_ID, "1h")
    assert len(series) == 1  # unique on (instrument, timeframe, start_time)
    assert series[0].close == 3390
    assert series[0].updated_at >= series[0].created_at


def test_instrument_upsert_and_lookup(tmp_path):
    repo = SqliteInstrumentRepository(make_store(tmp_path))
    inst = Instrument(ins_code="46348559193224090", isin="IRO1FOLD0001", symbol="فولاد")
    saved = repo.upsert(inst)
    again = repo.upsert(Instrument(ins_code="46348559193224090", isin="IRO1FOLD0001",
                                   symbol="فولاد", name="Foolad"))
    assert saved.id == again.id  # same natural key -> same internal ID
    assert repo.get_by_ins_code("46348559193224090").name == "Foolad"
    assert repo.get_by_isin("IRO1FOLD0001").symbol == "فولاد"
    assert [i.symbol for i in repo.get_by_symbol("فولاد")] == ["فولاد"]


def test_symbol_not_unique(tmp_path):
    repo = SqliteInstrumentRepository(make_store(tmp_path))
    repo.upsert(Instrument(ins_code="A1", symbol="X"))
    repo.upsert(Instrument(ins_code="A2", symbol="X"))  # same symbol, different instrument
    assert len(repo.get_by_symbol("X")) == 2


def test_activate_deactivate(tmp_path):
    repo = SqliteInstrumentRepository(make_store(tmp_path))
    inst = repo.upsert(Instrument(ins_code="A1", symbol="X"))
    repo.set_active(inst.id, False)
    assert repo.list_active() == []
    assert repo.get(inst.id).active is False


def test_fetch_run_lifecycle(tmp_path):
    repo = SqliteFetchRunRepository(make_store(tmp_path))
    fid = repo.start("tsetmc", "get_trades", "46348559193224090", date(2026, 9, 23))
    repo.finish(fid, status="OK", records_received=21174, duration_ms=1500)
    assert repo.has_success("tsetmc", "get_trades", "46348559193224090", date(2026, 9, 23))
    assert repo.last_success("tsetmc", "get_trades") is not None

    fid2 = repo.start("tsetmc", "get_trades", "BAD", date(2026, 9, 23))
    repo.finish(fid2, status="FAILED", error="ProviderTimeoutError: x")
    fails = repo.failures_since(datetime(2000, 1, 1))
    assert len(fails) == 1 and "ProviderTimeoutError" in fails[0]["error"]
