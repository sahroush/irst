from fastapi.testclient import TestClient
from iran_market.bootstrap import build
from iran_market.config import Settings
from iran_market.dashboard.app import create_app
from iran_market.models import Candle, CandleStatus, Instrument, LiveSnapshotState, Signal
from iran_market.monitoring.health import HealthService
from iran_market.timeutil import TEHRAN
from datetime import datetime, timedelta


def test_dashboard_empty_and_instrument(tmp_path):
    c=build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    c.instruments_repo.upsert(Instrument(symbol="فولاد",isin="IRO1TEST0001"))
    client=TestClient(create_app(c))
    assert client.get("/").status_code==200
    h=client.get("/api/health").json(); assert h["active_instruments"]==1
    rows=client.get("/api/instruments?q=فولاد").json(); assert rows[0]["symbol"]=="فولاد"
    assert client.get(f"/api/instruments/{rows[0]['id']}").status_code == 200
    page = client.get("/")
    assert 'id="market-chart"' in page.text
    assert 'Global timeframe' in page.text
    assert 'class="side-stack"' in page.text
    script = client.get('/static/dashboard.js').text
    assert 'class="instrument-select"' in script
    assert 'limit=500' not in script
    assert '/static/dashboard.css' in page.text
    assert '/static/dashboard.js' in page.text
    assert client.get('/static/dashboard.css').status_code == 200
    assert client.get('/static/dashboard.js').status_code == 200
    vendor = client.get('/static/vendor/lightweight-charts.standalone.production.js', headers={"Accept-Encoding": "gzip"})
    assert vendor.status_code == 200
    assert vendor.headers["content-encoding"] == "gzip"
    assert client.get('/static/vendor/VERSION.txt').text.strip() == 'Lightweight Charts 4.2.3'


def test_chart_api_returns_chronological_signal_markers(tmp_path):
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    starts = [datetime(2026, 9, 28, hour, 0, tzinfo=TEHRAN) for hour in (9, 10, 11)]
    for index, start in enumerate(starts):
        c.candles_repo.upsert(Candle(
            instrument_id=instrument.id, timeframe="30m", start_time=start,
            end_time=start.replace(minute=30), open=100 + index, high=110 + index,
            low=90 + index, close=105 + index, volume=1000 * (index + 1),
            trade_count=10 + index, status=CandleStatus.CLOSED,
        ))
    for index, start in enumerate(reversed(starts)):
        c.signals_repo.upsert(Signal(
            id=f"signal-{index}", scanner_name="supertrend", instrument_id=instrument.id,
            timeframe="30m", candle_time=start, signal_type="supertrend_flip",
            direction="bullish" if start == starts[0] else "bearish",
        ))
    response = TestClient(create_app(c)).get(f"/api/instruments/{instrument.id}?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert [candle["open"] for candle in data["candles"]] == [100, 101, 102]
    assert [marker["time"] for marker in data["markers"]] == [
        int(start.timestamp()) for start in starts
    ]
    assert [marker["position"] for marker in data["markers"]] == ["belowBar", "aboveBar", "aboveBar"]
    assert len(data["supertrend_series"]) == len(data["candles"])
    assert set(data["candles"][0]) == {"start_time", "end_time", "timeframe", "open", "high", "low", "close", "volume", "status"}
    assert [c.open for c in c.candles_repo.get_series(instrument.id, "30m", limit=2)] == [101, 102]


def test_global_daily_timeframe_resamples_bars_and_indicator_input(tmp_path):
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    for day in (28, 29):
        for slot in range(7):
            start = datetime(2026, 9, day, 9 + slot // 2, (slot % 2) * 30, tzinfo=TEHRAN)
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="30m", start_time=start,
                end_time=start + timedelta(minutes=30), open=100 + slot,
                high=110 + slot, low=90 + slot, close=105 + slot,
                volume=100 * (slot + 1), trade_count=slot + 1, status=CandleStatus.CLOSED,
            ))

    client = TestClient(create_app(c))
    response = client.get(f"/api/instruments/{instrument.id}?timeframe=1d")
    assert response.status_code == 200
    data = response.json()
    assert [(bar["timeframe"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"])
            for bar in data["candles"]] == [("1d", 100, 116, 90, 111, 2800)] * 2
    assert len(data["supertrend_series"]) == 2
    assert data["supertrend"]["timeframe"] == "1d"
    assert client.get(f"/api/instruments/{instrument.id}?timeframe=1h").json()["supertrend"]["timeframe"] == "1h"
    assert client.get('/api/config').json()["available_timeframes"] == ["30m", "1h", "1d"]
    assert 'id="timeframe" aria-label="Global timeframe"' in client.get("/").text
    listed = client.get('/api/instruments?timeframe=1d').json()[0]
    assert listed["close"] == 111 and listed["timeframe"] == "1d"
    daily_signals = client.get('/api/signals?timeframe=1d')
    assert daily_signals.status_code == 200
    assert all(signal["timeframe"] == "1d" for signal in daily_signals.json())
    hourly = client.get(f"/api/instruments/{instrument.id}?timeframe=1h").json()["candles"]
    assert [(bar["start_time"], bar["open"], bar["close"], bar["volume"]) for bar in hourly] == [
        (f"2026-09-{day}T{hour}:00:00+03:30", open_, close, volume)
        for day in ("28", "29")
        for hour, open_, close, volume in (("09", 100, 106, 300), ("10", 102, 108, 700),
                                           ("11", 104, 110, 1100), ("12", 106, 111, 700))
    ]


def test_dashboard_config_exposes_only_stored_timeframes(tmp_path):
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    c.candles_repo.upsert(Candle(
        instrument_id=instrument.id, timeframe="30m",
        start_time=datetime(2026, 9, 28, 9, 0, tzinfo=TEHRAN),
        end_time=datetime(2026, 9, 28, 9, 30, tzinfo=TEHRAN),
        open=1, high=2, low=1, close=2, volume=3, trade_count=1,
    ))
    client = TestClient(create_app(c))
    assert client.get('/api/config').json() == {
        "scanner_timeframe": c.settings.scanner_timeframe,
        "available_timeframes": ["30m", "1h", "1d"],
    }
    assert client.get(f'/api/instruments/{instrument.id}?timeframe=5m').status_code == 422


def test_derived_daily_signals_and_markers_use_daily_supertrend_flips(tmp_path):
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    day = datetime(2026, 9, 28, tzinfo=TEHRAN)
    days = []
    while len(days) < 12:
        if day.weekday() in (5, 6, 0, 1, 2):
            days.append(day)
        day += timedelta(days=1)

    for index, day in enumerate(days):
        for slot in range(7):
            start = day.replace(hour=9 + slot // 2, minute=(slot % 2) * 30)
            close = 120 if index == 11 else 100
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="30m", start_time=start,
                end_time=start + timedelta(minutes=30), open=100, high=121 if index == 11 else 101,
                low=99, close=close, volume=1, trade_count=1, status=CandleStatus.CLOSED,
            ))

    client = TestClient(create_app(c))
    signals = client.get('/api/signals?timeframe=1d').json()
    assert [(signal["direction"], signal["candle_time"]) for signal in signals] == [
        ("bullish", days[-1].replace(hour=9).isoformat())
    ]
    detail = client.get(f"/api/instruments/{instrument.id}?timeframe=1d").json()
    assert [(marker["position"], marker["time"]) for marker in detail["markers"]] == [
        ("belowBar", int(days[-1].replace(hour=9).timestamp()))
    ]


def test_closed_market_health_checks_last_session_snapshot(tmp_path, monkeypatch):
    from iran_market.monitoring import health

    now = datetime(2026, 9, 28, 13, 0, tzinfo=TEHRAN)
    monkeypatch.setattr(health, "now_tehran", lambda: now)
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    instrument = c.instruments_repo.list_active()[0]

    c.live_state_repo.upsert(LiveSnapshotState(
        instrument_id=instrument.id,
        timestamp=datetime(2026, 9, 28, 11, 0, tzinfo=TEHRAN),
    ))
    assert HealthService(c).report()["status"] == "DEGRADED"

    c.live_state_repo.upsert(LiveSnapshotState(
        instrument_id=instrument.id,
        timestamp=datetime(2026, 9, 28, 12, 59, 30, tzinfo=TEHRAN),
    ))
    assert HealthService(c).report()["status"] == "HEALTHY"
