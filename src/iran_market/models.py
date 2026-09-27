"""Pydantic v2 domain models. Independent of provider response schemas.

Prices/volumes are ints (rial rial prices are integral). If upstream ever
sends a fractional price, `_int_price` rounds and normalizers log a warning.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .timeutil import TEHRAN, now_tehran


def _int_price(v: Any) -> int | None:
    """Coerce 2-decimal float ('3350.00') or int to int rials."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return int(round(float(v)))


class Instrument(BaseModel):
    """Tradable instrument. Identified internally by UUID; InsCode/ISIN are aliases.

    ins_code is nullable from Milestone 2: WebGW-sourced instruments have ISIN
    only; InsCode filled later from tsetmc_legacy mapping. Never fabricated.
    """

    model_config = ConfigDict(ser_json_timedelta="iso8601")

    id: UUID = Field(default_factory=uuid4)
    ins_code: str | None = None
    isin: str | None = None
    symbol: str | None = None
    name: str | None = None
    market: str | None = None
    instrument_type: str | None = None
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_tehran)
    updated_at: datetime = created_at

    @property
    def has_tsetmc_mapping(self) -> bool:
        """True when a real (never fabricated) InsCode exists for TSETMC APIs."""
        return bool(self.ins_code)

    def matches(self, ins_code: str) -> bool:
        return self.ins_code == ins_code


class Trade(BaseModel):
    """One executed trade, normalized."""

    instrument_id: UUID
    trade_date: date
    timestamp: datetime  # tz-aware Tehran
    sequence: int  # nTran (unique per instrument+date per provider; not globally)
    price: int  # rials, integer (from _int_price)
    volume: int  # qTitTran
    canceled: bool = False
    source: str  # provider name, e.g. 'tsetmc'

    @property
    def key(self) -> tuple[UUID, date, int]:
        """Raw-identity key used for dedup/idempotency: (instrument, date, sequence)."""
        return (self.instrument_id, self.trade_date, self.sequence)


class CandleStatus(str, enum.Enum):
    BUILDING = "BUILDING"
    CLOSED = "CLOSED"
    RECONCILED = "RECONCILED"


class Candle(BaseModel):
    instrument_id: UUID
    timeframe: str  # '1m', '5m', '15m', '1h', '1d'
    start_time: datetime  # tz-aware; start of bucket, Tehran-anchored
    end_time: datetime  # exclusive end
    open: int
    high: int
    low: int
    close: int
    volume: int
    trade_count: int
    status: CandleStatus = CandleStatus.CLOSED
    source: str = ""
    created_at: datetime = Field(default_factory=now_tehran)
    updated_at: datetime = Field(default_factory=now_tehran)
    reconciled_at: datetime | None = None


class MarketSnapshot(BaseModel):
    """Bulk market state for one instrument at one moment (from market-watch)."""

    instrument_id: UUID | None  # None until registry lookup succeeds
    ins_code: str | None = None
    isin: str | None = None
    symbol: str | None = None
    timestamp: datetime = Field(default_factory=now_tehran)
    last_price: int | None = None
    closing_price: int | None = None
    yesterday_price: int | None = None
    first_price: int | None = None
    high: int | None = None
    low: int | None = None
    cumulative_volume: int | None = None
    cumulative_value: int | None = None
    trade_count: int | None = None
    state: str | None = None
    source: str = ""
    raw: dict[str, Any] | None = None


class LiveSnapshotState(BaseModel):
    """Last-seen cumulative counters per instrument (persisted for restart recovery).

    Volume deltas for live candles depend on previous cumulative values; this
    state survives scanner restarts so deltas continue correctly.
    """

    instrument_id: UUID
    timestamp: datetime
    cumulative_volume: int | None = None
    cumulative_value: int | None = None
    trade_count: int | None = None
    last_price: int | None = None


class Signal(BaseModel):
    """Scanner-detected condition. Idempotent via deterministic `id`."""

    id: str  # deterministic: sha1(scanner|instrument|timeframe|candle_time|type|params_hash)
    scanner_name: str
    instrument_id: UUID
    timeframe: str
    candle_time: datetime
    signal_type: str  # e.g. 'supertrend_flip'
    direction: str | None = None  # 'bullish' | 'bearish'
    indicator_name: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_tehran)
