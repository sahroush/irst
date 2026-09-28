"""Repository interfaces. SQL/serialization stays inside implementations."""

from __future__ import annotations

import abc
from datetime import date, datetime

from ..models import Candle, Instrument, Trade


class InstrumentRepository(abc.ABC):
    @abc.abstractmethod
    def upsert(self, instrument: Instrument) -> Instrument: ...

    @abc.abstractmethod
    def get(self, instrument_id) -> Instrument | None: ...

    @abc.abstractmethod
    def get_by_ins_code(self, ins_code: str) -> Instrument | None: ...

    @abc.abstractmethod
    def get_by_isin(self, isin: str) -> Instrument | None: ...

    @abc.abstractmethod
    def get_by_symbol(self, symbol: str) -> list[Instrument]: ...

    @abc.abstractmethod
    def list_active(self, market: str | None = None, instrument_type: str | None = None) -> list[Instrument]: ...

    @abc.abstractmethod
    def set_active(self, instrument_id, active: bool) -> None: ...


class TradeRepository(abc.ABC):
    @abc.abstractmethod
    def upsert_many(self, trades: list[Trade]) -> int: ...

    @abc.abstractmethod
    def get_trades(self, instrument_id, day: date) -> list[Trade]: ...

    @abc.abstractmethod
    def count_trades(self, instrument_id, day: date) -> int: ...


class CandleRepository(abc.ABC):
    @abc.abstractmethod
    def upsert(self, candle: Candle) -> None: ...

    @abc.abstractmethod
    def upsert_many(self, candles: list[Candle]) -> int: ...

    @abc.abstractmethod
    def get(self, instrument_id, timeframe: str, start_time: datetime) -> Candle | None: ...

    @abc.abstractmethod
    def get_series(self, instrument_id, timeframe: str, start: datetime | None = None,
                   end: datetime | None = None, limit: int | None = None) -> list[Candle]: ...


class FetchRunRepository(abc.ABC):
    @abc.abstractmethod
    def start(self, provider: str, operation: str, instrument: str | None,
              requested_date: date | None) -> int: ...

    @abc.abstractmethod
    def finish(self, fetch_id: int, *, status: str, http_status: int | None = None,
               records_received: int | None = None, attempt: int | None = None,
               error: str | None = None, duration_ms: int | None = None) -> None: ...

    @abc.abstractmethod
    def last_success(self, provider: str, operation: str, instrument: str | None = None,
                     requested_date: date | None = None) -> datetime | None: ...

    @abc.abstractmethod
    def has_success(self, provider: str, operation: str, instrument: str,
                    requested_date: date) -> bool: ...

    @abc.abstractmethod
    def failures_since(self, since: datetime) -> list[dict]: ...


class LiveStateRepository(abc.ABC):
    """Last-seen cumulative snapshot counters per instrument."""

    @abc.abstractmethod
    def upsert(self, state) -> None: ...

    @abc.abstractmethod
    def get(self, instrument_id): ...

    @abc.abstractmethod
    def get_all(self) -> dict: ...

    @abc.abstractmethod
    def clear(self) -> int: ...


class SignalRepository(abc.ABC):
    @abc.abstractmethod
    def upsert(self, signal) -> bool:
        """Insert; returns False if duplicate (idempotent)."""

    @abc.abstractmethod
    def get_latest(self, limit: int = 50, scanner_name: str | None = None) -> list: ...

    @abc.abstractmethod
    def list_for_instrument(self, instrument_id, timeframe: str | None = None) -> list: ...

    @abc.abstractmethod
    def list_recent(self, since: datetime, scanner_name: str | None = None) -> list: ...

    @abc.abstractmethod
    def list_by_scanner(self, scanner_name: str, limit: int = 100) -> list: ...
