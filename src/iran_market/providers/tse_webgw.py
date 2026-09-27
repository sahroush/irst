"""TSE Web Gateway provider (webgw.tse.ir) — bulk cash-market watch.

Verified behavior (2026-09-26):
- GET /InstrumentProvider/api/v1/MarketWatch/MarketWatchCash/fa returns
  {"Items": [...586 rows...]}, one row per instrument, one request for the
  whole cash market.
- Numeric fields may be plain numbers OR wrapped: {"value": x, "state": n}.
  Some wrappers carry null value. Non-wrapped plain numbers also occur
  (minValue/maxValue). Nulls are common (no order, no board, ...).
- Row identifiers: instrumentId (ISIN, e.g. 'IRO3ASPZ0001'), instrumentName
  (Persian short name), companyNamePersian. No InsCode in this response.
- Market fields: marketid/marketname (e.g. 30/'فرابورس'), stateid/statename
  (e.g. 'A '/'مجاز'), securitiesname ('سهام عادی').
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..http import HttpClient
from ..logging_setup import get_logger
from ..models import Instrument, MarketSnapshot
from ..timeutil import now_tehran
from .base import MarketDataProvider

log = get_logger("tse_webgw")

PROVIDER_NAME = "tse_webgw"

MARKET_WATCH_PATH = "/InstrumentProvider/api/v1/MarketWatch/MarketWatchCash/fa"

# Fields that arrive wrapped in {"value": .., "state": ..} in observed schema.
_WRAPPED_FIELDS = (
    "tradeVolume", "tradeValue", "tradeCount", "highValue", "lowValue",
    "lastPrice", "lastPriceChange", "lastPriceChangePercent", "closingPrice",
    "closingPriceChange", "closingPriceChangePercent", "yesterdayPrice",
    "firstPrice", "pe", "eps", "marketValue", "sellPrice", "buyPrice",
    "sellQuantity", "buyQuantity", "sellCount", "buyCount",
)


def unwrap(value):
    """Tolerate {"value": x, "state": y} wrappers; pass through anything else."""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def to_int(value) -> int | None:
    v = unwrap(value)
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def to_float(value) -> float | None:
    v = unwrap(value)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_str(value) -> str | None:
    v = unwrap(value)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


class TseWebGwProvider(MarketDataProvider):
    """Bulk cash-market watch + instrument discovery from webgw.tse.ir."""

    name = PROVIDER_NAME

    def __init__(self, http: HttpClient, base_url: str, known_schema_fields: set[str] | None = None) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._seen_fields: set[str] = known_schema_fields or set(_WRAPPED_FIELDS) | {
            "instrumentId", "instrumentName", "companyNamePersian", "marketid",
            "marketname", "markettypeid", "markettypename", "activemarkettypeid",
            "activemarkettypename", "industryid", "industryname", "industrysubid",
            "industrysubname", "securitiesid", "securitiesname", "stateid",
            "statename", "instrumentstatusid", "instrumentstatusname", "effectonIndex",
            "boardId", "boardName", "minValue", "maxValue",
        }

    def capabilities(self) -> frozenset[str]:
        return frozenset({"instruments", "market_snapshot"})

    async def get_market_watch_items(self) -> list[dict]:
        """Return raw bulk market-watch rows through a public provider API.

        Registry synchronization needs metadata that is not part of the normalized
        :class:`MarketSnapshot` model. Keeping this method here avoids callers
        reaching into provider-private HTTP/base-url attributes.
        """
        payload = await self._http.request_json(f"{self._base}{MARKET_WATCH_PATH}")
        items = self._extract_items(payload)
        self._log_schema_drift(items)
        return items

    async def get_market_snapshot(self, ins_codes: list[str] | None = None) -> list[MarketSnapshot]:
        """One bulk request -> MarketSnapshot list (all cash market; filter optional).

        ins_codes filters to a watch universe if given; webgw has no InsCode so
        filtering happens by ISIN upstream-free here: caller passes ISINs via
        `isin_filter` on the raw snapshots instead. Kept simple: returns all.
        """
        items = await self.get_market_watch_items()
        snapshots = []
        for idx, row in enumerate(items):
            try:
                snapshots.append(self._normalize_row(row, idx))
            except Exception as exc:  # unknown row shape: skip, don't fail bulk
                log.warning("skipping malformed market-watch row", extra={"row": idx, "error": str(exc)})
        log.info("market watch fetched", extra={"items": len(items), "normalized": len(snapshots)})
        return snapshots

    async def get_instruments(self):
        """Yield instruments discovered from the bulk watch (ISIN-keyed)."""
        for idx, row in enumerate(await self.get_market_watch_items()):
            isin = to_str(row.get("instrumentId"))
            if not isin:
                continue
            yield Instrument(
                ins_code=None,  # WebGW has no TSETMC InsCode; registry maps later
                isin=isin,
                symbol=to_str(row.get("instrumentName")),
                name=to_str(row.get("companyNamePersian")),
                market=to_str(row.get("marketname")),
                instrument_type=to_str(row.get("securitiesname")),
                active=True,
                metadata={"provider": PROVIDER_NAME},
            )

    # ------------------------------------------------------------- parsing

    @staticmethod
    def _extract_items(payload) -> list[dict]:
        if not isinstance(payload, dict):
            raise ValueError(f"expected object payload, got {type(payload).__name__}")
        items = payload.get("Items")
        if not isinstance(items, list):
            raise ValueError("missing or non-list 'Items'")
        return items

    def _normalize_row(self, row: dict, idx: int) -> MarketSnapshot:
        ts = now_tehran()
        return MarketSnapshot(
            instrument_id=None,
            isin=to_str(row.get("instrumentId")),
            symbol=to_str(row.get("instrumentName")),
            timestamp=ts,
            last_price=to_int(row.get("lastPrice")),
            closing_price=to_int(row.get("closingPrice")),
            yesterday_price=to_int(row.get("yesterdayPrice")),
            first_price=to_int(row.get("firstPrice")),
            high=to_int(row.get("highValue")),
            low=to_int(row.get("lowValue")),
            cumulative_volume=to_int(row.get("tradeVolume")),
            cumulative_value=to_int(row.get("tradeValue")),
            trade_count=to_int(row.get("tradeCount")),
            state=to_str(row.get("statename")),
            source=PROVIDER_NAME,
            raw=None,
        )

    def _log_schema_drift(self, items: list[dict]) -> None:
        """Log unknown fields once each — schema may change without notice."""
        new_fields = set()
        for row in items[:50]:
            new_fields.update(k for k in row.keys() if k not in self._seen_fields)
        if new_fields:
            log.info("market-watch schema: unknown fields (add to _WRAPPED_FIELDS if numeric)",
                     extra={"fields": sorted(new_fields)})
            self._seen_fields |= new_fields
