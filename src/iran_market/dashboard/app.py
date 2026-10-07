"""Read-only MVP dashboard for registry, candles, signals and health."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..bootstrap import build
from ..candles.models import Timeframe
from ..indicators.base import provisional_candles
from ..indicators.supertrend import SupertrendIndicator
from ..indicators.rsi import rsi_confirms, rsi_series
from ..models import Candle, CandleStatus
from ..monitoring.health import HealthService

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
    if timeframe in stored or timeframe not in {"1h", "1d"} or "30m" not in stored:
        return c.candles_repo.get_series(instrument_id, timeframe, limit=limit) if timeframe in stored else []
    target = Timeframe(timeframe)
    is_daily = target.is_daily
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
        output = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            direction = row["direction"]
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
