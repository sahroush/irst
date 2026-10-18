"""Read-only MVP dashboard for registry, candles, signals and health."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..bootstrap import build
from ..candles.models import Timeframe
from ..indicators.base import provisional_candles
from ..indicators.ema import ema_series
from ..indicators.supertrend import SupertrendIndicator
from ..indicators.rsi import rsi_confirms, rsi_series
from ..models import Candle, CandleStatus
from ..monitoring.health import HealthService
from ..storage.sqlite import latest_client_types

STATIC_DIR = Path(__file__).with_name("static")


def _stored_timeframes(c) -> set[str]:
    return {row["timeframe"] for row in c.store.connect().execute(
        "SELECT DISTINCT timeframe FROM candles"
    ).fetchall()}


def _timeframes(stored: set[str], default: str | None = None) -> list[str]:
    values = stored | ({"1h", "1d"} if "30m" in stored else set())
    if default:
        values.add(default)
    order = {"m": 0, "h": 1, "d": 2}
    return sorted(values, key=lambda code: (order[code[-1]], int(code[:-1])))


def _intraday_flips(indicator, instrument_id, daily: list[Candle], sub_bars: list[Candle]) -> list[tuple[str, str, bool]]:
    """Supertrend flips inside a still-open higher-timeframe bar, with their own time.

    A derived 1d/1h bar is stamped with its window start, so scanning the
    coarse series alone reports every flip of the current day at 09:00 and
    keeps at most one per day. Replaying the source bars into the partial bar
    shows when the state actually changed (09:00, 10:30, 11:00 ...) and keeps
    repeats.

    Each flip is confirmed by the RSI *as of that flip*, not by the coarse
    bar's final RSI: the bar's end-of-day reading authorises nothing that
    happened hours earlier. Returns (iso_time, state, rsi_confirmed).
    """
    if len(daily) < 2 or not sub_bars:
        return []
    prior, current = daily[:-1], daily[-1]
    states, _ = indicator._series(provisional_candles(prior)) if prior else ([], [])
    previous = states[-1] if states else None
    high = low = None
    volume = 0
    flips: list[tuple[str, str, bool]] = []
    for bar in sub_bars:
        high = bar.high if high is None else max(high, bar.high)
        low = bar.low if low is None else min(low, bar.low)
        volume += bar.volume or 0
        partial = Candle(
            instrument_id=instrument_id, timeframe=current.timeframe,
            start_time=current.start_time, end_time=bar.end_time,
            open=bar.open, high=high, low=low, close=bar.close,
            volume=volume, trade_count=0, status=CandleStatus.BUILDING, source="derived",
        )
        closed = provisional_candles(list(prior) + [partial])
        series, _ = indicator._series(closed)
        state = series[-1]
        if previous is not None and state != previous and "UNKNOWN" not in (state, previous):
            # RSI recomputed over the closes known at this flip only.
            rsi_now = rsi_series([float(b.close) for b in closed])
            direction = "bullish" if state == "BULLISH" else "bearish"
            confirmed = rsi_confirms(
                direction, rsi_now[-1],
                rsi_now[-2] if len(rsi_now) > 1 else None,
            )
            flips.append((bar.start_time.isoformat(), state, confirmed))
        previous = state
    return flips


def _chart_series(c, instrument_id, timeframe: str, limit: int, stored: set[str]) -> list[Candle]:
    """Candle series for one instrument.

    1d is special: stored EOD bars (official exchange daily rows, stamped
    09:00, no intraday data) merge with bars derived from 30m, which cover
    the recent window and today's in-progress bar. EOD bars strictly older
    than the first 30m day are kept; the derived view wins for overlapping
    days so the provisional bar and intraday flips stay intact.
    """
    if timeframe == "1d":
        eod = c.candles_repo.get_series(instrument_id, "1d", limit=limit) if "1d" in stored else []
        if "30m" not in stored:
            return eod[-limit:]
        recent = _derive_from_30m(c, instrument_id, Timeframe("1d"), limit)
        recent_days = {bar.start_time.date().isoformat() for bar in recent}
        # EOD fills the days 30m does not cover: deep history before coverage
        # began AND gap days inside the window (serve downtime, holidays).
        # Days 30m covers stay derived so today's provisional bar survives.
        kept = [bar for bar in eod if bar.start_time.date().isoformat() not in recent_days]
        return sorted(kept + recent, key=lambda bar: bar.start_time)[-limit:]
    if timeframe in stored:
        return c.candles_repo.get_series(instrument_id, timeframe, limit=limit)
    if timeframe in {"1h"} and "30m" in stored:
        return _derive_from_30m(c, instrument_id, Timeframe(timeframe), limit)
    return []


def _derive_from_30m(c, instrument_id, target: Timeframe, limit: int) -> list[Candle]:
    is_daily = target.is_daily
    timeframe = target.code
    unit, amount = timeframe[-1], int(timeframe[:-1])
    source = c.candles_repo.get_series(instrument_id, "30m", limit=limit * (7 if is_daily else 2))
    grouped = {}
    for candle in source:
        day = candle.start_time.date()
        window = c.calendar.day_window(day) if target.is_daily else None
        edges = ([(window.start, window.end)] if window else
                 c.calendar.hour_boundaries(day, amount) if unit == "h" else [])
        for start, end in edges:
            if start <= candle.start_time < end:
                grouped.setdefault((start, end), []).append(candle)
                break
    result = []
    for (start, end), bars in sorted(grouped.items()):
        result.append(Candle(
            instrument_id=bars[0].instrument_id, timeframe=timeframe,
            start_time=start, end_time=end, open=bars[0].open,
            high=max(bar.high for bar in bars), low=min(bar.low for bar in bars),
            close=bars[-1].close, volume=sum(bar.volume for bar in bars),
            trade_count=sum(bar.trade_count for bar in bars),
            status=(CandleStatus.BUILDING if any(bar.status == CandleStatus.BUILDING for bar in bars)
                    else CandleStatus.RECONCILED if all(bar.status == CandleStatus.RECONCILED for bar in bars)
                    else CandleStatus.CLOSED), source="derived",
            created_at=bars[0].created_at, updated_at=max(bar.updated_at for bar in bars),
        ))
    return result[-limit:]


def _liquidity(c, day: str | None = None) -> dict[str, tuple[float, int]]:
    """Settled traded value (rial) and distinct closes per instrument for a day.

    Tradeability gate grounded in the data: small names trade 2-9B rial/day
    and limit-locked names print one price all session (candle high-low range
    is 0.00% for them, so range cannot detect the lock - distinct closes can).
    Uses CLOSED 30m bars only: the BUILDING bar carries cumulative day volume,
    so including it double-counts. `day=None` means the latest session that
    has closed 30m bars, with a fallback to the previous session when the
    current one has barely started (opening minutes would otherwise drop
    fresh signals from liquid names).
    """
    conn = c.store.connect()
    if day is None:
        day = conn.execute(
            "SELECT MAX(substr(start_time,1,10)) FROM candles WHERE timeframe='30m' AND status='CLOSED'"
        ).fetchone()[0]
    rows = conn.execute(
        """SELECT instrument_id, SUM(volume*close) as value, COUNT(DISTINCT close) as closes
           FROM candles
           WHERE timeframe='30m' AND status='CLOSED' AND substr(start_time,1,10)=?
           GROUP BY instrument_id""",
        (day,),
    ).fetchall()
    gate = {row["instrument_id"]: (row["value"] or 0.0, row["closes"]) for row in rows}
    if len(gate) < 50:
        prev = conn.execute(
            """SELECT MAX(substr(start_time,1,10)) FROM candles
               WHERE timeframe='30m' AND status='CLOSED' AND substr(start_time,1,10)<?""",
            (day,),
        ).fetchone()[0]
        if prev:
            for row in conn.execute(
                """SELECT instrument_id, SUM(volume*close) as value, COUNT(DISTINCT close) as closes
                   FROM candles
                   WHERE timeframe='30m' AND status='CLOSED' AND substr(start_time,1,10)=?
                   GROUP BY instrument_id""",
                (prev,),
            ).fetchall():
                gate.setdefault(row["instrument_id"], (row["value"] or 0.0, row["closes"]))
    return gate


def _tradeable(c, gate: dict[str, tuple[float, int]], instrument_id) -> bool:
    """True when the instrument clears both tradeability thresholds."""
    value, closes = gate.get(str(instrument_id), (0.0, 0))
    return (value >= c.settings.min_traded_value
            and closes >= c.settings.min_price_discovery)


EMA_SCREENS = {"price_ema6", "ema6_ema11", "ema11_ema21"}
BUYER_POWER_RATIO = 1.5

# The three EMA tabs share one per-instrument daily snapshot (series + EMAs):
# ~1s of work for the whole market, so serve it from a short-TTL cache and
# let each tab just filter it. TTL < the 30s poll, so in-progress bars stay
# honest; the cache is per-process (fine for the single serve instance).
# The lock prevents a rebuild stampede: without it, N concurrent requests on
# an expired TTL each rebuild (~1s of SQLite-heavy work apiece, serialized
# by the DB -> N-second waits).
_EMA_CACHE_TTL = 10.0
_ema_cache: dict[str, tuple[float, dict]] = {"snapshot": (0.0, {})}
_ema_cache_lock = threading.Lock()


def _ema_snapshot(c, stored: set[str]) -> dict[str, dict]:
    """Per-instrument {close, ema6, ..., prev_close, ..., candle_time} for the market.

    Current and previous bar values so tabs can detect crosses, not states.
    Shared by the three EMA tabs and cached briefly: building it walks every
    tradeable instrument's daily series (~1s), while each tab only filters.
    """
    stamp, snapshot = _ema_cache["snapshot"]
    if snapshot and time.monotonic() - stamp < _EMA_CACHE_TTL:
        return snapshot
    with _ema_cache_lock:
        stamp, snapshot = _ema_cache["snapshot"]
        if snapshot and time.monotonic() - stamp < _EMA_CACHE_TTL:
            return snapshot  # another request already rebuilt it
        gate = _liquidity(c)
        snapshot = {}
        for instrument in c.instruments_repo.list_active():
            if not _tradeable(c, gate, instrument.id):
                continue
            candles = _chart_series(c, instrument.id, "1d", 120, stored)
            usable = provisional_candles(candles)
            if not usable:
                continue
            closes = [float(bar.close) for bar in usable]
            e6 = ema_series(closes, 6)
            e11 = ema_series(closes, 11)
            e21 = ema_series(closes, 21)
            snapshot[str(instrument.id)] = {
                "symbol": instrument.symbol,
                "close": closes[-1],
                "ema6": e6[-1], "ema11": e11[-1], "ema21": e21[-1],
                "prev_close": closes[-2] if len(closes) > 1 else None,
                "prev_ema6": e6[-2] if len(closes) > 1 else None,
                "prev_ema11": e11[-2] if len(closes) > 1 else None,
                "prev_ema21": e21[-2] if len(closes) > 1 else None,
                "candle_time": usable[-1].start_time.isoformat(),
            }
        _ema_cache["snapshot"] = (time.monotonic(), snapshot)
        return snapshot


def _ema_cache_reset():
    """Drop the shared EMA snapshot (tests use it between data mutations)."""
    _ema_cache["snapshot"] = (0.0, {})


def _ema_screen(c, kind: str, stored: set[str]) -> list[dict]:
    """Instruments where the daily EMA pair crossed upward on the latest bar.

    A signal only on the transition (was below/equal, now above), not every
    bar the condition merely holds on. The in-progress day participates via
    provisional_candles, so a cross can fire intraday; the close can still
    reverse it before session end.
    """
    pair = {"price_ema6": ("close", "ema6"), "ema6_ema11": ("ema6", "ema11"), "ema11_ema21": ("ema11", "ema21")}[kind]
    left_key, right_key = pair
    prev_left_key, prev_right_key = "prev_" + left_key, "prev_" + right_key
    rows = []
    for instrument_id, s in _ema_snapshot(c, stored).items():
        left, right = s[left_key], s[right_key]
        prev_left, prev_right = s[prev_left_key], s[prev_right_key]
        if not (left and right):
            continue
        # cross signal: currently above, and not above on the previous bar
        if not (left > right and not (prev_left and prev_right and prev_left > prev_right)):
            continue
        left, right = s[pair[0]], s[pair[1]]
        rows.append({
            "instrument_id": instrument_id, "symbol": s["symbol"],
            "close": s["close"], "ema6": s["ema6"], "ema11": s["ema11"], "ema21": s["ema21"],
            "value": left / right if right else None,
            "candle_time": s["candle_time"],
        })
    return sorted(rows, key=lambda row: (row["value"] or 0), reverse=True)


def _buyer_power_screen(c) -> list[dict]:
    """قدرت خریدار حقیقی > 1.5 × قدرت فروشنده حقیقی on the latest stored day.

    Tradeability-gated like the other screens (min settled value + price
    discovery) so the tab lists only liquid names.
    """
    gate = _liquidity(c)
    rows = []
    for row in latest_client_types(c.store):
        if not _tradeable(c, gate, row.get("instrument_id")):
            continue
        bp = row["buy_i_volume"] / row["buy_i_count"] if row["buy_i_count"] else None
        sp = row["sell_i_volume"] / row["sell_i_count"] if row["sell_i_count"] else None
        if bp is None or sp is None or bp <= BUYER_POWER_RATIO * sp:
            continue
        rows.append({
            "instrument_id": row.get("instrument_id"), "symbol": row.get("symbol"),
            "buy_power": bp, "sell_power": sp,
            "value": bp / sp if sp else None, "candle_time": row["day"],
        })
    return sorted(rows, key=lambda row: (row["value"] or 0), reverse=True)


def _timeframe_signals(c, timeframe: str, stored: set[str], limit: int, instrument_id=None) -> list[dict]:
    if timeframe == c.settings.scanner_timeframe and timeframe in stored:
        sql = """SELECT s.*, i.symbol FROM signals s
                 LEFT JOIN instruments i ON i.id=s.instrument_id
                 WHERE s.timeframe=?"""
        params: list[str | int] = [timeframe]
        if instrument_id is not None:
            sql += " AND s.instrument_id=?"
            params.append(str(instrument_id))
        sql += " ORDER BY s.candle_time DESC LIMIT ?"
        params.append(limit)
        rows = c.store.connect().execute(sql, params).fetchall()
        gate = _liquidity(c)
        output = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            direction = row["direction"]
            if not _tradeable(c, gate, row["instrument_id"]):
                continue
            if rsi_confirms(direction, payload.get("rsi"), payload.get("previous_rsi")):
                output.append({
                    "id": row["id"], "instrument_id": row["instrument_id"], "symbol": row["symbol"],
                    "direction": direction, "candle_time": row["candle_time"],
                    "provisional": False, "confirmed": True,
                    "scanner_name": row["scanner_name"], "timeframe": timeframe,
                })
        return output

    indicator, signals = SupertrendIndicator(), []
    instruments = c.instruments_repo.list_active()
    if instrument_id is not None:
        instruments = [item for item in instruments if item.id == instrument_id]
    else:
        # Small or limit-locked names cannot be filled either side: skip them
        # before any indicator work instead of filtering signals afterwards.
        gate = _liquidity(c)
        instruments = [item for item in instruments if _tradeable(c, gate, item.id)]
    for instrument in instruments:
        candles = _chart_series(c, instrument.id, timeframe, 120, stored)
        # The in-progress period participates on available prices, so the
        # current day/30m/1h can alert before its candle closes.
        usable = provisional_candles(candles)
        states, _ = indicator._series(usable) if usable else ([], [])
        rsi_values = rsi_series([float(bar.close) for bar in usable])
        # A derived bar carries its window's start time, so a coarse scan would
        # report each flip of the open period at 09:00 and drop the ones that
        # follow it. Replay the source bars so every intraday flip is kept and
        # carries the time it actually happened.
        intraday: list[tuple[str, str, bool]] = []
        if timeframe not in stored and candles and candles[-1].start_time == usable[-1].start_time:
            subs = c.candles_repo.get_series(
                instrument.id, "30m", start=candles[-1].start_time,
                end=candles[-1].end_time,
            )
            intraday = _intraday_flips(indicator, instrument.id, candles, subs)
        for index in range(1, len(usable)):
            if states[index] == states[index - 1] or "UNKNOWN" in (states[index], states[index - 1]):
                continue
            direction = "bullish" if states[index] == "BULLISH" else "bearish"
            base = usable[index].start_time.isoformat()
            # The open bar's flips are provisional: the day can still invalidate
            # each one before it closes, so report every transition with its own
            # RSI verdict instead of letting the closing bar authorise all of them.
            if base == usable[-1].start_time.isoformat() and intraday:
                for stamp, state, confirmed in intraday:
                    signals.append({
                        "id": f"{instrument.id}:{timeframe}:{stamp}",
                        "instrument_id": str(instrument.id), "symbol": instrument.symbol,
                        "direction": "bullish" if state == "BULLISH" else "bearish",
                        "candle_time": stamp,
                        "provisional": True, "confirmed": confirmed,
                        "scanner_name": "supertrend", "timeframe": timeframe,
                    })
                continue
            if not rsi_confirms(direction, rsi_values[index], rsi_values[index - 1]):
                continue
            signals.append({
                "id": f"{instrument.id}:{timeframe}:{base}",
                "instrument_id": str(instrument.id), "symbol": instrument.symbol,
                "direction": direction,
                "candle_time": base,
                "provisional": False, "confirmed": True,
                "scanner_name": "supertrend", "timeframe": timeframe,
            })
    return sorted(signals, key=lambda signal: signal["candle_time"], reverse=True)[:limit]


def create_app(container=None) -> FastAPI:
    c = container or build()
    app = FastAPI(title="Iran Market Scanner Dashboard", version="0.1.0")
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.state.container = c
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # index.html and the scripts it pulls in are edited as one unit, so a
        # cached script against fresh markup breaks the page silently. Let the
        # browser store the asset but always revalidate: StaticFiles answers
        # 304 when the file is unchanged, so the vendored chart bundle still
        # costs one conditional request.
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def root():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health(): return HealthService(c).report()

    @app.get("/api/config")
    def config():
        stored = _stored_timeframes(c)
        return {"scanner_timeframe": c.settings.scanner_timeframe, "available_timeframes": _timeframes(stored, c.settings.scanner_timeframe)}

    @app.get("/api/instruments")
    def instruments(limit: int = Query(100, ge=1, le=1000), q: str = "", timeframe: str | None = None):
        stored = _stored_timeframes(c)
        tf = timeframe or c.settings.scanner_timeframe
        if tf not in _timeframes(stored, c.settings.scanner_timeframe):
            raise HTTPException(422, "unsupported timeframe")
        if tf in stored:
            sql = """SELECT i.*, c.close, c.status candle_status, c.updated_at candle_updated
                     FROM instruments i LEFT JOIN candles c ON c.rowid=(SELECT c2.rowid FROM candles c2
                     WHERE c2.instrument_id=i.id AND c2.timeframe=? ORDER BY c2.start_time DESC LIMIT 1)
                     WHERE i.active=1"""
            params: list[str | int] = [tf]
            if q:
                sql += " AND (i.symbol LIKE ? OR i.name LIKE ? OR i.isin LIKE ?)"
                params.extend([f"%{q}%"] * 3)
            sql += " ORDER BY i.symbol LIMIT ?"
            params.append(limit)
            rows = c.store.connect().execute(sql, params).fetchall()
            return [{
                "id": row["id"], "isin": row["isin"], "ins_code": row["ins_code"],
                "symbol": row["symbol"], "name": row["name"], "market": row["market"],
                "close": row["close"], "candle_status": row["candle_status"], "timeframe": tf,
                "updated_at": row["candle_updated"] or row["updated_at"],
            } for row in rows]

        rows = c.instruments_repo.list_active()
        if q:
            query = q.casefold()
            rows = [row for row in rows if query in " ".join((row.symbol or "", row.name or "", row.isin or "")).casefold()]
        output = []
        for instrument in sorted(rows, key=lambda row: row.symbol or "")[:limit]:
            latest = _chart_series(c, instrument.id, tf, 1, stored)
            candle = latest[-1] if latest else None
            output.append({
                "id": str(instrument.id), "isin": instrument.isin, "ins_code": instrument.ins_code,
                "symbol": instrument.symbol, "name": instrument.name, "market": instrument.market,
                "close": candle.close if candle else None,
                "candle_status": candle.status.value if candle else None, "timeframe": tf,
                "updated_at": candle.updated_at.isoformat() if candle else instrument.updated_at.isoformat(),
            })
        return output

    @app.get("/api/signals")
    def signals(limit: int = Query(50, ge=1, le=500), timeframe: str | None = None):
        stored = _stored_timeframes(c)
        tf = timeframe or c.settings.scanner_timeframe
        if tf not in _timeframes(stored, c.settings.scanner_timeframe):
            raise HTTPException(422, "unsupported timeframe")
        return _timeframe_signals(c, tf, stored, limit)

    @app.get("/api/screen")
    def screen(kind: str, limit: int = Query(100, ge=1, le=500)):
        stored = _stored_timeframes(c)
        if kind in EMA_SCREENS:
            return _ema_screen(c, kind, stored)[:limit]
        if kind == "buyer_power":
            return _buyer_power_screen(c)[:limit]
        raise HTTPException(422, f"unknown screen kind: {kind}")

    @app.get("/api/instruments/{instrument_id}")
    def instrument_detail(instrument_id: str, timeframe: str | None = None, limit: int = Query(120, ge=10, le=1000)):
        stored = _stored_timeframes(c)
        timeframe = timeframe or c.settings.scanner_timeframe
        if timeframe not in _timeframes(stored, c.settings.scanner_timeframe):
            raise HTTPException(422, "unsupported timeframe")
        inst=c.instruments_repo.get(instrument_id)
        if inst is None: raise HTTPException(404,"instrument not found")
        cs=_chart_series(c, inst.id, timeframe, limit, stored)
        ind=SupertrendIndicator()
        # The in-progress candle participates on available prices: live state,
        # trend and RSI extend to the current period instead of stopping at
        # the last closed bar.
        live = provisional_candles(cs)
        result=ind.calculate(live)
        _, live_st = ind._series(live) if live else ([], [])
        live_by_time = {x.start_time: value for x, value in zip(live, live_st)}
        live_rsi = rsi_series([float(x.close) for x in live])
        rsi_by_time = {x.start_time: value for x, value in zip(live, live_rsi)}
        markers = []
        candle_times = {int(x.start_time.timestamp()) for x in cs}
        for signal in _timeframe_signals(c, timeframe, stored, 500, inst.id):
            stamp = int(datetime.fromisoformat(signal["candle_time"]).timestamp())
            if stamp not in candle_times or signal["direction"] not in ("bullish", "bearish"):
                continue
            bullish = signal["direction"] == "bullish"
            markers.append({
                "time": stamp, "position": "belowBar" if bullish else "aboveBar",
                "color": "#29c78a" if bullish else "#f06472",
                "shape": "arrowUp" if bullish else "arrowDown",
                "text": "BUY" if bullish else "SELL",
            })
        markers.sort(key=lambda marker: marker["time"])
        return {
            "instrument": inst.model_dump(mode="json"),
            "candles": [{
                "start_time": x.start_time.isoformat(), "end_time": x.end_time.isoformat(),
                "timeframe": x.timeframe, "open": x.open, "high": x.high, "low": x.low, "close": x.close,
                "volume": x.volume, "status": x.status.value,
            } for x in cs],
            "supertrend": {"timeframe": timeframe, "state": result.state, "value": result.values.get("supertrend"), "atr": result.values.get("atr"), "valid": result.valid},
            "supertrend_series": [live_by_time.get(x.start_time) for x in cs],
            "rsi_series": [rsi_by_time.get(x.start_time) for x in cs],
            "markers": markers,
        }

    return app

app = create_app()
