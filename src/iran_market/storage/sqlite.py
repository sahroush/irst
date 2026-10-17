"""SQLite development storage. Same UPSERT semantics PostgreSQL will get later.

Uniqueness per spec: candles (instrument_id, timeframe, start_time);
trades (instrument_id, trade_date, sequence).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from uuid import UUID

from ..errors import StorageError
from ..logging_setup import get_logger
from ..models import Candle, CandleStatus, Instrument, Trade
from .base import (
    CandleRepository,
    FetchRunRepository,
    InstrumentRepository,
    LiveStateRepository,
    SignalRepository,
    TradeRepository,
)

log = get_logger("sqlite")


def _dt(v: str | None) -> datetime | None:
    if v is None:
        return None
    return datetime.fromisoformat(v)


def _d(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


class SqliteStore:
    """Connection holder. One file, WAL mode, per-thread connections."""

    def __init__(self, path: str | Path) -> None:
        if isinstance(path, str) and "://" in path:
            path = path.split("sqlite:///", 1)[-1]
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = _thread_local()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn


def _thread_local():
    import threading
    return threading.local()


SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    id TEXT PRIMARY KEY,
    ins_code TEXT,
    isin TEXT UNIQUE,
    symbol TEXT,
    name TEXT,
    market TEXT,
    instrument_type TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_instruments_ins_code ON instruments(ins_code) WHERE ins_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_instruments_symbol ON instruments(symbol);

CREATE TABLE IF NOT EXISTS trades (
    instrument_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    ts TEXT NOT NULL,
    price INTEGER NOT NULL,
    volume INTEGER NOT NULL,
    canceled INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    PRIMARY KEY (instrument_id, trade_date, sequence)
);
CREATE INDEX IF NOT EXISTS ix_trades_date ON trades(trade_date);

CREATE TABLE IF NOT EXISTS candles (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    open INTEGER NOT NULL,
    high INTEGER NOT NULL,
    low INTEGER NOT NULL,
    close INTEGER NOT NULL,
    volume INTEGER NOT NULL,
    trade_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reconciled_at TEXT,
    PRIMARY KEY (instrument_id, timeframe, start_time)
);
CREATE INDEX IF NOT EXISTS ix_candles_timeframe ON candles(timeframe);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    instrument TEXT,
    requested_date TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    records_received INTEGER,
    attempt INTEGER,
    error TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fetch_runs_lookup ON fetch_runs(provider, operation, instrument, requested_date);

CREATE TABLE IF NOT EXISTS live_snapshot_state (
    instrument_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    cumulative_volume INTEGER,
    cumulative_value INTEGER,
    trade_count INTEGER,
    last_price INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    scanner_name TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_time TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    direction TEXT,
    indicator_name TEXT,
    parameters TEXT NOT NULL DEFAULT '{}',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signals_instrument ON signals(instrument_id, timeframe, candle_time);
CREATE INDEX IF NOT EXISTS ix_signals_scanner ON signals(scanner_name, candle_time);

CREATE TABLE IF NOT EXISTS client_types (
    ins_code TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    buy_i_count INTEGER, buy_i_volume INTEGER, sell_i_count INTEGER, sell_i_volume INTEGER,
    buy_l_count INTEGER, buy_l_volume INTEGER, sell_l_count INTEGER, sell_l_volume INTEGER,
    fetched_at TEXT NOT NULL
);
"""


class SqliteInstrumentRepository(InstrumentRepository):
    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def upsert(self, instrument: Instrument) -> Instrument:
        """Resolve natural key: ISIN first, then InsCode; keep original id/created_at.

        Never overwrites an existing non-null ins_code with NULL (mapping
        preservation rule).
        """
        conn = self._store.connect()
        with conn:
            existing = None
            if instrument.isin:
                existing = conn.execute(
                    "SELECT * FROM instruments WHERE isin = ?", (instrument.isin,)
                ).fetchone()
            if existing is None and instrument.ins_code:
                existing = conn.execute(
                    "SELECT * FROM instruments WHERE ins_code = ?", (instrument.ins_code,)
                ).fetchone()
            if existing:
                existing_isin = existing["isin"]
                existing_ins_code = existing["ins_code"]
                # mapping preservation: don't clobber a known InsCode with NULL
                ins_code = instrument.ins_code or existing_ins_code
                isin = instrument.isin or existing_isin
                instrument = instrument.model_copy(
                    update={
                        "id": UUID(existing["id"]),
                        "ins_code": ins_code,
                        "isin": isin,
                        "created_at": _dt(existing["created_at"]),
                        "updated_at": datetime.now(instrument.updated_at.tzinfo),
                    }
                )
            conn.execute(
                """INSERT INTO instruments (id, ins_code, isin, symbol, name, market,
                       instrument_type, active, metadata, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       ins_code=excluded.ins_code, isin=excluded.isin, symbol=excluded.symbol,
                       name=excluded.name, market=excluded.market,
                       instrument_type=excluded.instrument_type, active=excluded.active,
                       metadata=excluded.metadata, updated_at=excluded.updated_at""",
                (
                    str(instrument.id), instrument.ins_code, instrument.isin, instrument.symbol,
                    instrument.name, instrument.market, instrument.instrument_type,
                    int(instrument.active), json.dumps(instrument.metadata, ensure_ascii=False),
                    instrument.created_at.isoformat(), instrument.updated_at.isoformat(),
                ),
            )
        return instrument

    def _row_to_instrument(self, r: sqlite3.Row) -> Instrument:
        return Instrument(
            id=UUID(r["id"]), ins_code=r["ins_code"], isin=r["isin"], symbol=r["symbol"],
            name=r["name"], market=r["market"], instrument_type=r["instrument_type"],
            active=bool(r["active"]), metadata=json.loads(r["metadata"]),
            created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
        )

    def get(self, instrument_id) -> Instrument | None:
        r = self._store.connect().execute("SELECT * FROM instruments WHERE id = ?", (str(instrument_id),)).fetchone()
        return self._row_to_instrument(r) if r else None

    def get_by_ins_code(self, ins_code: str) -> Instrument | None:
        r = self._store.connect().execute("SELECT * FROM instruments WHERE ins_code = ?", (ins_code,)).fetchone()
        return self._row_to_instrument(r) if r else None

    def get_by_isin(self, isin: str) -> Instrument | None:
        r = self._store.connect().execute("SELECT * FROM instruments WHERE isin = ?", (isin,)).fetchone()
        return self._row_to_instrument(r) if r else None

    def get_by_symbol(self, symbol: str) -> list[Instrument]:
        rows = self._store.connect().execute(
            "SELECT * FROM instruments WHERE symbol = ?", (symbol,)
        ).fetchall()
        return [self._row_to_instrument(r) for r in rows]

    def list_active(self, market: str | None = None, instrument_type: str | None = None) -> list[Instrument]:
        q = "SELECT * FROM instruments WHERE active = 1"
        params: list = []
        if market:
            q += " AND market = ?"
            params.append(market)
        if instrument_type:
            q += " AND instrument_type = ?"
            params.append(instrument_type)
        return [self._row_to_instrument(r) for r in self._store.connect().execute(q, params).fetchall()]

    def set_active(self, instrument_id, active: bool) -> None:
        conn = self._store.connect()
        with conn:
            conn.execute(
                "UPDATE instruments SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), datetime.now().isoformat(), str(instrument_id)),
            )


class SqliteTradeRepository(TradeRepository):
    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def upsert_many(self, trades: list[Trade]) -> int:
        if not trades:
            return 0
        conn = self._store.connect()
        inserted_or_updated = 0
        with conn:
            for t in trades:
                cur = conn.execute(
                    """INSERT INTO trades (instrument_id, trade_date, sequence, ts, price,
                           volume, canceled, source)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(instrument_id, trade_date, sequence) DO UPDATE SET
                           ts=excluded.ts, price=excluded.price, volume=excluded.volume,
                           canceled=excluded.canceled, source=excluded.source""",
                    (
                        str(t.instrument_id), t.trade_date.isoformat(), t.sequence,
                        t.timestamp.isoformat(), t.price, t.volume, int(t.canceled), t.source,
                    ),
                )
                inserted_or_updated += cur.rowcount if cur.rowcount > 0 else 0
        return inserted_or_updated

    def get_trades(self, instrument_id, day: date) -> list[Trade]:
        rows = self._store.connect().execute(
            "SELECT * FROM trades WHERE instrument_id = ? AND trade_date = ? ORDER BY ts, sequence",
            (str(instrument_id), day.isoformat()),
        ).fetchall()
        return [
            Trade(
                instrument_id=UUID(r["instrument_id"]), trade_date=_d(r["trade_date"]),
                timestamp=_dt(r["ts"]), sequence=r["sequence"], price=r["price"],
                volume=r["volume"], canceled=bool(r["canceled"]), source=r["source"],
            )
            for r in rows
        ]

    def count_trades(self, instrument_id, day: date) -> int:
        r = self._store.connect().execute(
            "SELECT COUNT(*) AS n FROM trades WHERE instrument_id = ? AND trade_date = ?",
            (str(instrument_id), day.isoformat()),
        ).fetchone()
        return r["n"]


class SqliteCandleRepository(CandleRepository):
    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def upsert(self, candle: Candle) -> None:
        conn = self._store.connect()
        with conn:
            conn.execute(
                """INSERT INTO candles (instrument_id, timeframe, start_time, end_time,
                       open, high, low, close, volume, trade_count, status, source,
                       created_at, updated_at, reconciled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(instrument_id, timeframe, start_time) DO UPDATE SET
                       end_time=excluded.end_time, open=excluded.open, high=excluded.high,
                       low=excluded.low, close=excluded.close, volume=excluded.volume,
                       trade_count=excluded.trade_count, status=excluded.status,
                       source=excluded.source, updated_at=excluded.updated_at,
                       reconciled_at=excluded.reconciled_at""",
                (
                    str(candle.instrument_id), candle.timeframe, candle.start_time.isoformat(),
                    candle.end_time.isoformat(), candle.open, candle.high, candle.low,
                    candle.close, candle.volume, candle.trade_count, candle.status.value,
                    candle.source, candle.created_at.isoformat(), candle.updated_at.isoformat(),
                    candle.reconciled_at.isoformat() if candle.reconciled_at else None,
                ),
            )

    def upsert_many(self, candles: list[Candle]) -> int:
        n = 0
        for c in candles:
            self.upsert(c)
            n += 1
        return n

    def get(self, instrument_id, timeframe: str, start_time: datetime) -> Candle | None:
        r = self._store.connect().execute(
            "SELECT * FROM candles WHERE instrument_id = ? AND timeframe = ? AND start_time = ?",
            (str(instrument_id), timeframe, start_time.isoformat()),
        ).fetchone()
        return self._row_to_candle(r) if r else None

    def get_series(self, instrument_id, timeframe: str, start: datetime | None = None,
                   end: datetime | None = None, limit: int | None = None) -> list[Candle]:
        q = "SELECT * FROM candles WHERE instrument_id = ? AND timeframe = ?"
        params: list = [str(instrument_id), timeframe]
        if start:
            q += " AND start_time >= ?"
            params.append(start.isoformat())
        if end:
            q += " AND start_time < ?"
            params.append(end.isoformat())
        q += " ORDER BY start_time"
        if limit is not None:
            rows = self._store.connect().execute(
                f"SELECT * FROM ({q} DESC LIMIT ?) ORDER BY start_time", [*params, limit]
            ).fetchall()
        else:
            rows = self._store.connect().execute(q, params).fetchall()
        return [self._row_to_candle(r) for r in rows]

    @staticmethod
    def _row_to_candle(r: sqlite3.Row) -> Candle:
        return Candle(
            instrument_id=UUID(r["instrument_id"]), timeframe=r["timeframe"],
            start_time=_dt(r["start_time"]), end_time=_dt(r["end_time"]),
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r["volume"], trade_count=r["trade_count"],
            status=CandleStatus(r["status"]), source=r["source"],
            created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
            reconciled_at=_dt(r["reconciled_at"]),
        )


class SqliteFetchRunRepository(FetchRunRepository):
    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def start(self, provider: str, operation: str, instrument: str | None,
              requested_date: date | None) -> int:
        conn = self._store.connect()
        with conn:
            cur = conn.execute(
                "INSERT INTO fetch_runs (provider, operation, instrument, requested_date, started_at, status)"
                " VALUES (?,?,?,?,?,?)",
                (provider, operation, instrument,
                 requested_date.isoformat() if requested_date else None,
                 datetime.now().isoformat(), "RUNNING"),
            )
        return int(cur.lastrowid)

    def finish(self, fetch_id: int, *, status: str, http_status: int | None = None,
               records_received: int | None = None, attempt: int | None = None,
               error: str | None = None, duration_ms: int | None = None) -> None:
        conn = self._store.connect()
        with conn:
            conn.execute(
                """UPDATE fetch_runs SET finished_at=?, status=?, http_status=?,
                       records_received=?, attempt=?, error=?, duration_ms=? WHERE id=?""",
                (datetime.now().isoformat(), status, http_status, records_received,
                 attempt, error, duration_ms, fetch_id),
            )

    def last_success(self, provider: str, operation: str, instrument: str | None = None,
                     requested_date: date | None = None) -> datetime | None:
        q = "SELECT MAX(finished_at) AS t FROM fetch_runs WHERE status='OK'"
        params: list = []
        for col, val in (("provider", provider), ("operation", operation),
                         ("instrument", instrument),
                         ("requested_date", requested_date.isoformat() if requested_date else None)):
            if val is not None:
                q += f" AND {col} = ?"
                params.append(val)
        r = self._store.connect().execute(q, params).fetchone()
        return _dt(r["t"]) if r and r["t"] else None

    def has_success(self, provider: str, operation: str, instrument: str,
                    requested_date: date) -> bool:
        r = self._store.connect().execute(
            "SELECT 1 FROM fetch_runs WHERE provider=? AND operation=? AND instrument=?"
            " AND requested_date=? AND status='OK' LIMIT 1",
            (provider, operation, instrument, requested_date.isoformat()),
        ).fetchone()
        return r is not None

    def failures_since(self, since: datetime) -> list[dict]:
        rows = self._store.connect().execute(
            "SELECT * FROM fetch_runs WHERE status != 'OK' AND started_at >= ? ORDER BY started_at DESC LIMIT 200",
            (since.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]


class SqliteLiveStateRepository(LiveStateRepository):
    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def upsert(self, state) -> None:
        conn = self._store.connect()
        with conn:
            conn.execute(
                """INSERT INTO live_snapshot_state (instrument_id, ts, cumulative_volume,
                       cumulative_value, trade_count, last_price)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(instrument_id) DO UPDATE SET
                       ts=excluded.ts, cumulative_volume=excluded.cumulative_volume,
                       cumulative_value=excluded.cumulative_value,
                       trade_count=excluded.trade_count, last_price=excluded.last_price""",
                (
                    str(state.instrument_id), state.timestamp.isoformat(),
                    state.cumulative_volume, state.cumulative_value,
                    state.trade_count, state.last_price,
                ),
            )

    def get(self, instrument_id):
        r = self._store.connect().execute(
            "SELECT * FROM live_snapshot_state WHERE instrument_id = ?", (str(instrument_id),)
        ).fetchone()
        if r is None:
            return None
        from ..models import LiveSnapshotState
        return LiveSnapshotState(
            instrument_id=UUID(r["instrument_id"]), timestamp=_dt(r["ts"]),
            cumulative_volume=r["cumulative_volume"], cumulative_value=r["cumulative_value"],
            trade_count=r["trade_count"], last_price=r["last_price"],
        )

    def get_all(self) -> dict:
        rows = self._store.connect().execute("SELECT * FROM live_snapshot_state").fetchall()
        from ..models import LiveSnapshotState
        return {
            UUID(r["instrument_id"]): LiveSnapshotState(
                instrument_id=UUID(r["instrument_id"]), timestamp=_dt(r["ts"]),
                cumulative_volume=r["cumulative_volume"], cumulative_value=r["cumulative_value"],
                trade_count=r["trade_count"], last_price=r["last_price"],
            )
            for r in rows
        }

    def clear(self) -> int:
        conn = self._store.connect()
        with conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM live_snapshot_state").fetchone()["n"]
            conn.execute("DELETE FROM live_snapshot_state")
        return n


class SqliteSignalRepository(SignalRepository):
    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def upsert(self, signal) -> bool:
        conn = self._store.connect()
        with conn:
            cur = conn.execute(
                """INSERT INTO signals (id, scanner_name, instrument_id, timeframe,
                       candle_time, signal_type, direction, indicator_name, parameters,
                       payload, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO NOTHING""",
                (
                    signal.id, signal.scanner_name, str(signal.instrument_id),
                    signal.timeframe, signal.candle_time.isoformat(), signal.signal_type,
                    signal.direction, signal.indicator_name,
                    json.dumps(signal.parameters, ensure_ascii=False),
                    json.dumps(signal.payload, ensure_ascii=False),
                    signal.created_at.isoformat(),
                ),
            )
        return cur.rowcount > 0

    def get(self, signal_id: str):
        r = self._store.connect().execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        return self._row_to_signal(r) if r else None

    def delete(self, signal_id: str) -> bool:
        conn = self._store.connect()
        with conn:
            cur = conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
        return cur.rowcount > 0

    @staticmethod
    def _row_to_signal(r: sqlite3.Row):
        from ..models import Signal
        return Signal(
            id=r["id"], scanner_name=r["scanner_name"], instrument_id=UUID(r["instrument_id"]),
            timeframe=r["timeframe"], candle_time=_dt(r["candle_time"]),
            signal_type=r["signal_type"], direction=r["direction"],
            indicator_name=r["indicator_name"],
            parameters=json.loads(r["parameters"]), payload=json.loads(r["payload"]),
            created_at=_dt(r["created_at"]),
        )

    def get_latest(self, limit: int = 50, scanner_name: str | None = None) -> list:
        q = "SELECT * FROM signals"
        params: list = []
        if scanner_name:
            q += " WHERE scanner_name = ?"
            params.append(scanner_name)
        q += " ORDER BY candle_time DESC, created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_signal(r) for r in self._store.connect().execute(q, params).fetchall()]

    def list_for_instrument(self, instrument_id, timeframe: str | None = None) -> list:
        q = "SELECT * FROM signals WHERE instrument_id = ?"
        params: list = [str(instrument_id)]
        if timeframe:
            q += " AND timeframe = ?"
            params.append(timeframe)
        q += " ORDER BY candle_time DESC"
        return [self._row_to_signal(r) for r in self._store.connect().execute(q, params).fetchall()]

    def list_recent(self, since: datetime, scanner_name: str | None = None) -> list:
        q = "SELECT * FROM signals WHERE candle_time >= ?"
        params: list = [since.isoformat()]
        if scanner_name:
            q += " AND scanner_name = ?"
            params.append(scanner_name)
        q += " ORDER BY candle_time DESC"
        return [self._row_to_signal(r) for r in self._store.connect().execute(q, params).fetchall()]

    def list_by_scanner(self, scanner_name: str, limit: int = 100) -> list:
        rows = self._store.connect().execute(
            "SELECT * FROM signals WHERE scanner_name = ? ORDER BY candle_time DESC LIMIT ?",
            (scanner_name, limit),
        ).fetchall()
        return [self._row_to_signal(r) for r in rows]


def open_sqlite_store(database_url: str) -> SqliteStore:
    """'sqlite:///path' or bare path -> SqliteStore."""
    return SqliteStore(database_url)


def upsert_client_types(store: SqliteStore, breakdown, day: str) -> None:
    """One row per instrument, overwritten daily: the screen needs only the latest."""
    from ..timeutil import now_tehran
    conn = store.connect()
    with conn:
        conn.execute(
            """INSERT INTO client_types
                 (ins_code, day, buy_i_count, buy_i_volume, sell_i_count, sell_i_volume,
                  buy_l_count, buy_l_volume, sell_l_count, sell_l_volume, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ins_code) DO UPDATE SET
                 day=excluded.day, buy_i_count=excluded.buy_i_count,
                 buy_i_volume=excluded.buy_i_volume, sell_i_count=excluded.sell_i_count,
                 sell_i_volume=excluded.sell_i_volume, buy_l_count=excluded.buy_l_count,
                 buy_l_volume=excluded.buy_l_volume, sell_l_count=excluded.sell_l_count,
                 sell_l_volume=excluded.sell_l_volume, fetched_at=excluded.fetched_at""",
            (breakdown.ins_code, day,
             breakdown.buy_i_count, breakdown.buy_i_volume,
             breakdown.sell_i_count, breakdown.sell_i_volume,
             breakdown.buy_l_count, breakdown.buy_l_volume,
             breakdown.sell_l_count, breakdown.sell_l_volume,
             now_tehran().isoformat()),
        )


def latest_client_types(store: SqliteStore) -> list[dict]:
    """All rows of the most recent stored day, joined to instruments."""
    conn = store.connect()
    day = conn.execute("SELECT MAX(day) FROM client_types").fetchone()[0]
    if not day:
        return []
    return [dict(row) for row in conn.execute(
        """SELECT ct.*, i.id as instrument_id, i.symbol FROM client_types ct
           LEFT JOIN instruments i ON i.ins_code = ct.ins_code
           WHERE ct.day = ?""", (day,),
    ).fetchall()]


def prune_before(store: SqliteStore, days: int, *, now: datetime | None = None) -> dict:
    """Delete trades/fetch_runs older than `days`, then VACUUM.

    Candles and signals are kept forever: an OHLCV row is a few numbers while
    the trades it came from dominate DB size, and the EMA screens and
    indicators need long daily history. days <= 0 disables. All timestamps are
    Tehran-local ISO strings; comparisons are lexicographic and safe because
    every writer uses the same +03:30 offset.
    """
    if days is None or days <= 0:
        return {}
    from ..timeutil import now_tehran
    cutoff = (now or now_tehran()) - timedelta(days=days)
    conn = store.connect()
    report = {}
    with conn:
        report["trades"] = conn.execute(
            "DELETE FROM trades WHERE trade_date < ?", (cutoff.date().isoformat(),)
        ).rowcount
        report["fetch_runs"] = conn.execute(
            "DELETE FROM fetch_runs WHERE started_at < ?",
            (cutoff.replace(tzinfo=None).isoformat(),),
        ).rowcount
    if any(report.values()):
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    return report


def create_schema(store: SqliteStore) -> None:
    conn = store.connect()
    with conn:
        conn.executescript(SCHEMA)
