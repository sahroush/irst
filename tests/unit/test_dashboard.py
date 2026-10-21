from fastapi.testclient import TestClient
import pytest
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
    # Watchlist panel is gone; Supertrend flips is the only sidebar list and
    # the search box jumps straight to a match instead of listing results.
    assert 'instrument-rows' not in page.text
    assert 'Watchlist' not in page.text
    assert 'Supertrend flips' in page.text
    sidebar = page.text.split('side-stack', 1)[1]
    assert sidebar.count('<section class="panel') == 1
    script = client.get('/static/dashboard.js').text
    assert 'class="instrument-select"' in script
    assert 'renderInstruments' not in script
    assert "event.key === 'Enter'" in script
    assert 'limit=500' not in script
    assert '/static/dashboard.css' in page.text
    assert '/static/dashboard.js' in page.text
    assert client.get('/static/dashboard.css').status_code == 200
    assert client.get('/static/dashboard.js').status_code == 200
    vendor = client.get('/static/vendor/lightweight-charts.standalone.production.js', headers={"Accept-Encoding": "gzip"})
    assert vendor.status_code == 200
    assert vendor.headers["content-encoding"] == "gzip"
    assert client.get('/static/vendor/VERSION.txt').text.strip() == 'Lightweight Charts 4.2.3'
    # A stale cached dashboard.js against fresh index.html freezes the page on
    # "Loading…" with no message, so static assets must always revalidate.
    for path in ("/", "/static/dashboard.js", "/static/dashboard.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/health").headers


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
    assert data["markers"] == []
    assert len(data["supertrend_series"]) == len(data["candles"])
    assert len(data["rsi_series"]) == len(data["candles"])
    assert data["rsi_series"] == [None, None, None]
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
        "playlists": ["all", "main"],
    }
    assert client.get(f'/api/instruments/{instrument.id}?timeframe=5m').status_code == 422


def test_derived_daily_signals_and_markers_use_daily_supertrend_flips(tmp_path):
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    day = datetime(2026, 9, 28, tzinfo=TEHRAN)
    days = []
    while len(days) < 17:
        if day.weekday() in (5, 6, 0, 1, 2):
            days.append(day)
        day += timedelta(days=1)

    for index, day in enumerate(days):
        for slot in range(7):
            start = day.replace(hour=9 + slot // 2, minute=(slot % 2) * 30)
            close = 120 if index == 16 else 100
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="30m", start_time=start,
                end_time=start + timedelta(minutes=30), open=100, high=121 if index == 16 else 101,
                low=99, close=close, volume=1, trade_count=1, status=CandleStatus.CLOSED,
            ))

    client = TestClient(create_app(c))
    signals = client.get('/api/signals?timeframe=1d').json()
    assert [(signal["direction"], signal["candle_time"]) for signal in signals] == [
        ("bullish", days[-1].replace(hour=9).isoformat())
    ]
    detail = client.get(f"/api/instruments/{instrument.id}?timeframe=1d").json()
    assert len(detail["rsi_series"]) == len(detail["candles"])
    assert detail["rsi_series"][-1] is not None
    assert [(marker["position"], marker["time"]) for marker in detail["markers"]] == [
        ("belowBar", int(days[-1].replace(hour=9).timestamp()))
    ]


def test_derived_daily_signals_report_each_intraday_flip_time(tmp_path):
    """1d flips must keep their own time, not the daily bar's 09:00 start.

    The derived daily bar is stamped with the session window start, so a
    coarse scan reported every flip at 09:00 and kept only one per day. The
    intraday replay recovers the real flip time and keeps repeats.
    """
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    day = datetime(2026, 9, 28, tzinfo=TEHRAN)
    days = []
    while len(days) < 17:
        if day.weekday() in (5, 6, 0, 1, 2):
            days.append(day)
        day += timedelta(days=1)

    # Flat history, then an oscillating final day that flips several times.
    for index, day_ in enumerate(days[:-1]):
        for slot in range(7):
            start = day_.replace(hour=9 + slot // 2, minute=(slot % 2) * 30)
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="30m", start_time=start,
                end_time=start + timedelta(minutes=30), open=100, high=101, low=99,
                close=100, volume=1, trade_count=1, status=CandleStatus.CLOSED,
            ))
    # Oscillate: each 30m close flips the daily Supertrend state.
    closes = [120, 80, 130, 70, 140, 60, 150]
    for slot, close in enumerate(closes):
        start = days[-1].replace(hour=9 + slot // 2, minute=(slot % 2) * 30)
        c.candles_repo.upsert(Candle(
            instrument_id=instrument.id, timeframe="30m", start_time=start,
            end_time=start + timedelta(minutes=30), open=100, high=close + 5,
            low=close - 5, close=close, volume=1, trade_count=1,
            status=CandleStatus.CLOSED,
        ))

    client = TestClient(create_app(c))
    signals = client.get('/api/signals?timeframe=1d').json()
    times = sorted({signal["candle_time"] for signal in signals})
    # Flips are no longer all collapsed onto the daily bar start.
    assert len(times) > 1, f"all flips still share one timestamp: {times}"
    # The oscillating day is the newest bar, so every flip belongs to it.
    assert {t[:10] for t in times} == {days[-1].date().isoformat()}, times
    # Times are real session slots, and later than 09:00 is reachable.
    assert {t[11:16] for t in times} <= {
        "09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00",
    }
    assert any(t[11:16] != "09:00" for t in times), times


def test_open_bar_flips_carry_own_rsi_verdict_and_provisional_flag(tmp_path):
    """An intraday flip must be judged by RSI at its own time, not the bar's final RSI.

    Emitting every open-bar flip under the coarse bar's closing RSI authorised
    flips that their own RSI rejected (a bearish flip while RSI was still above
    50). Such rows must be flagged unconfirmed, never silently confirmed.
    """
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    day = datetime(2026, 9, 28, tzinfo=TEHRAN)
    days = []
    while len(days) < 20:
        if day.weekday() in (5, 6, 0, 1, 2):
            days.append(day)
        day += timedelta(days=1)

    # Falling history (RSI below 50), then a final day that dips and spikes:
    # the early bearish flip happens while the daily RSI is still too high for
    # a bearish confirmation, even though the day ends bullish.
    for d in days[:-1]:
        for slot in range(7):
            start = d.replace(hour=9 + slot // 2, minute=(slot % 2) * 30)
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="30m", start_time=start,
                end_time=start + timedelta(minutes=30), open=90, high=92, low=88,
                close=90, volume=1, trade_count=1, status=CandleStatus.CLOSED,
            ))
    for slot, close in enumerate([120, 90, 95, 100, 150, 200, 210]):
        start = days[-1].replace(hour=9 + slot // 2, minute=(slot % 2) * 30)
        status = CandleStatus.CLOSED if slot < 6 else CandleStatus.BUILDING
        c.candles_repo.upsert(Candle(
            instrument_id=instrument.id, timeframe="30m", start_time=start,
            end_time=start + timedelta(minutes=30), open=100, high=close + 6,
            low=close - 6, close=close, volume=1, trade_count=1, status=status,
        ))

    client = TestClient(create_app(c))
    signals = client.get('/api/signals?timeframe=1d').json()
    last_day = days[-1].date().isoformat()
    open_bar = [s for s in signals if s["candle_time"].startswith(last_day)]
    # Every transition inside the open bar is reported, in both directions.
    assert len(open_bar) > 1, [(s["candle_time"], s["direction"]) for s in open_bar]
    assert {s["direction"] for s in open_bar} == {"bullish", "bearish"}, open_bar
    for signal in open_bar:
        assert signal["provisional"] is True, signal
    # The 09:30 bearish flip is rejected by the RSI of that moment (RSI still
    # above 50). It must not inherit the closing bar's verdict, which confirms.
    early_bear = [s for s in open_bar if s["direction"] == "bearish" and s["candle_time"].endswith("09:30:00+03:30")]
    assert early_bear, open_bar
    assert early_bear[0]["confirmed"] is False, early_bear[0]
    # A flip that RSI did confirm is still distinguishable from the rejected one.
    assert any(s["confirmed"] is True for s in open_bar), open_bar


def test_current_daily_alert_uses_in_progress_bar(tmp_path):
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    day = datetime(2026, 9, 28, tzinfo=TEHRAN)
    days = []
    while len(days) < 17:
        if day.weekday() in (5, 6, 0, 1, 2):
            days.append(day)
        day += timedelta(days=1)

    for index, day in enumerate(days):
        for slot in range(7):
            start = day.replace(hour=9 + slot // 2, minute=(slot % 2) * 30)
            open_now = index == 16 and slot == 6  # today's last 30m bar is still open
            close = 120 if index == 16 else 100
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="30m", start_time=start,
                end_time=start + timedelta(minutes=30), open=100,
                high=121 if index == 16 else 101, low=99, close=close,
                volume=1, trade_count=1,
                status=CandleStatus.BUILDING if open_now else CandleStatus.CLOSED,
            ))

    client = TestClient(create_app(c))
    # The derived daily bar containing the in-progress 30m bar still signals.
    signals = client.get('/api/signals?timeframe=1d').json()
    assert [(signal["direction"], signal["candle_time"]) for signal in signals] == [
        ("bullish", days[-1].replace(hour=9).isoformat())
    ]
    detail = client.get(f"/api/instruments/{instrument.id}?timeframe=1d").json()
    assert detail["candles"][-1]["status"] == "BUILDING"
    assert detail["supertrend"]["state"] == "BULLISH"
    assert detail["supertrend_series"][-1] is not None
    assert detail["rsi_series"][-1] is not None
    assert [(marker["position"], marker["time"]) for marker in detail["markers"]] == [
        ("belowBar", int(days[-1].replace(hour=9).timestamp()))
    ]


def test_tradeability_gate_filters_small_and_locked_instruments(tmp_path):
    """Signals only from instruments that can actually be traded.

    Small names (tiny settled value) and limit-locked names (one price all
    session, no price discovery) cannot be filled either side. Both must be
    dropped from every signal path: persisted scanner signals and live
    derived-timeframe flips.
    """
    c = build(Settings.for_tests(
        database_url=f"sqlite:///{tmp_path/'db.sqlite'}",
        min_traded_value=100_000.0,
        min_price_discovery=3,
    ))
    liquid = c.instruments_repo.upsert(Instrument(symbol="فولاد", isin="IRO1TEST0001"))
    small = c.instruments_repo.upsert(Instrument(symbol="سپ", isin="IRO1TEST0002"))
    locked = c.instruments_repo.upsert(Instrument(symbol="وغدیر", isin="IRO1TEST0003"))
    day = datetime(2026, 9, 28, tzinfo=TEHRAN)
    days = []
    while len(days) < 17:
        if day.weekday() in (5, 6, 0, 1, 2):
            days.append(day)
        day += timedelta(days=1)

    def bars(instrument, day, closes, volume):
        for slot, close in enumerate(closes):
            start = day.replace(hour=9 + slot)
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="30m", start_time=start,
                end_time=start + timedelta(minutes=30), open=100, high=close + 5,
                low=close - 5, close=close, volume=volume, trade_count=1,
                status=CandleStatus.CLOSED,
            ))

    # Flat history (distinct days, one bar each) so Supertrend has state, then
    # three distinct closes on the final day so the instrument flips.
    for instrument in (liquid, small, locked):
        for prev in days[:-1]:
            bars(instrument, prev, [100], 1000)
    bars(liquid, days[-1], [120, 130, 140], 5000)   # value ~1.95M rial passes the 100k gate
    bars(small, days[-1], [120, 130, 140], 10)      # value ~3.9k rial fails
    bars(locked, days[-1], [240, 240, 240], 5000)   # one distinct close: locked, fails discovery

    # Persisted scanner path: plant one flip row per instrument (the scanner
    # runs separately from the dashboard) and let the gate filter them.
    from iran_market.models import Signal
    for instrument in (liquid, small, locked):
        c.store.connect().execute(
            "INSERT INTO signals (id, scanner_name, instrument_id, timeframe, candle_time, signal_type, direction, indicator_name, parameters, payload, created_at) VALUES (?, 'supertrend', ?, '30m', ?, 'supertrend_flip', 'bullish', 'supertrend', '{}', ?, ?)",
            (f"test-{instrument.id}", str(instrument.id), days[-1].replace(hour=9).isoformat(), '{"rsi": 60.0, "previous_rsi": 55.0}', datetime.now(TEHRAN).isoformat()),
        )
        c.store.connect().commit()

    client = TestClient(create_app(c))
    # 30m: persisted rows must be gated - only the liquid instrument survives.
    symbols = {s["symbol"] for s in client.get("/api/signals?timeframe=30m&limit=500").json()}
    assert "فولاد" in symbols, f"30m: liquid instrument must still signal, got {symbols}"
    assert "سپ" not in symbols, f"30m: small instrument must be dropped, got {symbols}"
    assert "وغدیر" not in symbols, f"30m: locked instrument must be dropped, got {symbols}"
    # 1d: live derived path must apply the same gate per instrument.
    symbols = {s["symbol"] for s in client.get("/api/signals?timeframe=1d&limit=500").json()}
    assert "فولاد" in symbols, f"1d: liquid instrument must still signal, got {symbols}"
    assert "سپ" not in symbols, f"1d: small instrument must be dropped, got {symbols}"
    assert "وغدیر" not in symbols, f"1d: locked instrument must be dropped, got {symbols}"


def test_liquidity_gate_falls_back_to_previous_session_at_open(tmp_path):
    """Opening minutes must not drop fresh signals from liquid names.

    The gate reads the latest session with closed 30m bars; when the current
    session has barely started (under 50 instruments with bars), it falls back
    to the previous session instead of reporting every instrument untradeable.
    """
    c = build(Settings.for_tests(
        database_url=f"sqlite:///{tmp_path/'db.sqlite'}",
        min_traded_value=100_000.0,
        min_price_discovery=3,
    ))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001"))
    # 17 sessions ending 2026-09-27: only that last day has bars in the DB
    # (earlier days are left empty), so the gate's latest-session query finds
    # exactly one instrument-day with data - the fallback keeps it tradeable.
    day0 = datetime(2026, 9, 27, tzinfo=TEHRAN)
    days = []
    day = day0
    while len(days) < 17:
        if day.weekday() in (5, 6, 0, 1, 2):
            days.append(day)
        day += timedelta(days=1)
    for prev in days[:-1]:
        start = prev.replace(hour=9)
        c.candles_repo.upsert(Candle(
            instrument_id=instrument.id, timeframe="30m", start_time=start,
            end_time=start + timedelta(minutes=30), open=100, high=105, low=95,
            close=100, volume=5000, trade_count=1, status=CandleStatus.CLOSED,
        ))
    for slot, close in enumerate((120, 130, 140)):
        start = days[-1].replace(hour=9 + slot)
        c.candles_repo.upsert(Candle(
            instrument_id=instrument.id, timeframe="30m", start_time=start,
            end_time=start + timedelta(minutes=30), open=100, high=close + 5,
            low=close - 5, close=close, volume=5000, trade_count=1,
            status=CandleStatus.CLOSED,
        ))

    client = TestClient(create_app(c))
    # No bars after the last session: the gate falls back to it and the
    # instrument still signals on the live derived path.
    signals = client.get("/api/signals?timeframe=1d&limit=500").json()
    assert any(s["symbol"] == "A" for s in signals), signals


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

def test_screen_endpoint_kinds_and_tabs(tmp_path):
    from iran_market.models import ClientTypeBreakdown
    from iran_market.storage.sqlite import upsert_client_types

    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="A", isin="IRO1TEST0001", ins_code="123"))
    # 30 real 1d bars (stored "1d" rows are read directly by _chart_series).
    # Long rise, then a sustained decline (puts price below all EMAs and
    # inverts every EMA pair), then one big close -> the last bar crosses
    # every pair upward.
    closes = [105 + i for i in range(23)] + [110 - 5 * i for i in range(1, 8)] + [200]
    for i, close in enumerate(closes):
        s = datetime(2026, 9, 1, tzinfo=TEHRAN) + timedelta(days=i)
        c.candles_repo.upsert(Candle(
            instrument_id=instrument.id, timeframe="1d", start_time=s,
            end_time=s + timedelta(hours=4), open=closes[0], high=close + 5,
            low=close - 15, close=close, volume=1000, trade_count=5,
            status=CandleStatus.CLOSED,
        ))
    client = TestClient(create_app(c))
    # Last bar crosses each pair upward (prev bar was below), so each screen
    # fires exactly once. The metric is the condition's LHS/RHS pair.
    for kind, lhs_key, rhs_key in (("price_ema6", "close", "ema6"),
                                   ("ema6_ema11", "ema6", "ema11"),
                                   ("ema11_ema21", "ema11", "ema21")):
        rows = client.get(f"/api/screen?kind={kind}").json()
        assert len(rows) == 1 and rows[0]["symbol"] == "A"
        assert rows[0]["value"] == pytest.approx(rows[0][lhs_key] / rows[0][rhs_key])
        assert rows[0]["value"] > 1.0
    # No cross when the condition merely keeps holding: append another high
    # bar and the screens go quiet again (state, not transition).
    c.candles_repo.upsert(Candle(
        instrument_id=instrument.id, timeframe="1d",
        start_time=datetime(2026, 10, 2, tzinfo=TEHRAN),
        end_time=datetime(2026, 10, 2, 4, tzinfo=TEHRAN),
        open=200, high=210, low=190, close=205, volume=1000, trade_count=5,
        status=CandleStatus.CLOSED,
    ))
    from iran_market.dashboard.app import _ema_cache_reset
    _ema_cache_reset()
    for kind in ("price_ema6", "ema6_ema11", "ema11_ema21"):
        assert client.get(f"/api/screen?kind={kind}").json() == []
    assert client.get("/api/screen?kind=nope").status_code == 422
    # buyer_power: individual buy power 2x sell power -> passes the 1.5 gate.
    upsert_client_types(c.store, ClientTypeBreakdown(
        ins_code="123", buy_i_count=10, buy_i_volume=1000,
        sell_i_count=10, sell_i_volume=500, source="tsetmc",
    ), day="2026-09-28")
    rows = client.get("/api/screen?kind=buyer_power").json()
    assert len(rows) == 1 and rows[0]["symbol"] == "A"
    assert rows[0]["buy_power"] == 100 and rows[0]["sell_power"] == 50
    # Page carries the tab bar with all four new tabs.
    page = client.get("/").text
    for tab in ("price_ema6", "ema6_ema11", "ema11_ema21", "buyer_power"):
        assert f'data-tab="{tab}"' in page


def test_playlists_filter_screens_and_instruments(tmp_path, monkeypatch):
    from iran_market.dashboard import app as app_module
    from iran_market.dashboard.app import _ema_cache_reset

    # Two instruments cross every EMA pair; the playlist names only "A".
    monkeypatch.setattr(app_module, "_load_playlists",
                        lambda: {"main": {"A"}})
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    for symbol in ("A", "B"):
        instrument = c.instruments_repo.upsert(Instrument(symbol=symbol, isin=f"IRO1TEST00{symbol}", ins_code=symbol))
        closes = [105 + i for i in range(23)] + [110 - 5 * i for i in range(1, 8)] + [200]
        for i, close in enumerate(closes):
            s = datetime(2026, 9, 1, tzinfo=TEHRAN) + timedelta(days=i)
            c.candles_repo.upsert(Candle(
                instrument_id=instrument.id, timeframe="1d", start_time=s,
                end_time=s + timedelta(hours=4), open=closes[0], high=close + 5,
                low=close - 15, close=close, volume=1000, trade_count=5,
                status=CandleStatus.CLOSED,
            ))
    client = TestClient(create_app(c))
    _ema_cache_reset()

    # Config lists the implicit "all" plus the named playlists.
    config = client.get("/api/config").json()
    assert config["playlists"] == ["all", "main"]

    # Unfiltered: both instruments screen. With playlist=main: only "A".
    rows = client.get("/api/screen?kind=price_ema6").json()
    assert {r["symbol"] for r in rows} == {"A", "B"}
    rows = client.get("/api/screen?kind=price_ema6&playlist=main").json()
    assert [r["symbol"] for r in rows] == ["A"]
    # Unknown playlist falls back to unfiltered (no silent blanking).
    rows = client.get("/api/screen?kind=price_ema6&playlist=missing").json()
    assert {r["symbol"] for r in rows} == {"A", "B"}

    # Same filter on the instruments list.
    rows = client.get("/api/instruments?timeframe=1d&limit=1000").json()
    assert {r["symbol"] for r in rows} == {"A", "B"}
    rows = client.get("/api/instruments?timeframe=1d&playlist=main").json()
    assert [r["symbol"] for r in rows] == ["A"]


def test_chart_series_merges_eod_with_derived_daily(tmp_path):
    """EOD backfill rows older than 30m coverage merge; overlap goes to 30m."""
    from datetime import date
    c = build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    instrument = c.instruments_repo.upsert(Instrument(symbol="B", isin="IRO1TESTB001", ins_code="456"))
    # EOD bar for a day with NO 30m data (2 months before 30m coverage starts)
    c.candles_repo.upsert(Candle(
        instrument_id=instrument.id, timeframe="1d",
        start_time=datetime(2026, 7, 1, 9, tzinfo=TEHRAN), end_time=datetime(2026, 7, 1, 13, tzinfo=TEHRAN),
        open=100, high=110, low=90, close=105, volume=10, trade_count=2,
        status=CandleStatus.RECONCILED, source="eod",
    ))
    # One session of 30m bars (3 closed bars + nothing else)
    for i in range(3):
        s = datetime(2026, 9, 1, 9 + i * 0 if False else 9, 0, tzinfo=TEHRAN) + timedelta(minutes=30 * i)
        c.candles_repo.upsert(Candle(
            instrument_id=instrument.id, timeframe="30m", start_time=s,
            end_time=s + timedelta(minutes=30), open=100 + i, high=110 + i,
            low=90 + i, close=105 + i, volume=5, trade_count=1,
            status=CandleStatus.CLOSED,
        ))
    from iran_market.dashboard.app import _chart_series, _stored_timeframes
    stored = _stored_timeframes(c)
    series = _chart_series(c, instrument.id, "1d", 120, stored)
    days = [bar.start_time.date().isoformat() for bar in series]
    assert days == ["2026-07-01", "2026-09-01"]
    assert series[0].source == "eod" and series[0].status == CandleStatus.RECONCILED
    assert series[1].source == "derived" and series[1].close == 107  # 105+1+... last 30m close
    # 1h still derives from 30m only (3 bars = 09:00-10:30 = 2 hour buckets)
    assert [b.start_time.hour for b in _chart_series(c, instrument.id, "1h", 10, stored)] == [9, 10]
