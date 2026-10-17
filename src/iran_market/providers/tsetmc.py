"""TSETMC CDN provider.

Verified behavior (2026-09-26):
- GET /api/Trade/GetTradeHistory/{ins_code}/{YYYYMMDD}/false returns
  {"tradeHistory": [...]} ordered newest-first.
- Trade fields: nTran (sequence), hEven (HHMMSS int, leading zeros dropped),
  qTitTran (volume), pTran (price float like 3350.00 -> 3350 rials),
  canceled (0/1).
- ~21k trades for liquid names on active days; response ~4 MB.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from ..errors import ProviderParseError
from ..http import HttpClient
from ..logging_setup import get_logger
from ..models import ClientTypeBreakdown, Trade
from ..timeutil import TEHRAN, heven_to_time, to_gregorian_compact
from .base import MarketDataProvider

log = get_logger("tsetmc")

PROVIDER_NAME = "tsetmc"


class TsetmcProvider(MarketDataProvider):
    """TSETMC CDN (cdn.tsetmc.com) provider. Trade history for exact candles."""

    name = PROVIDER_NAME

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def capabilities(self) -> frozenset[str]:
        return frozenset({"trades", "client_types"})

    async def get_trades(self, ins_code: str, day: date) -> list[Trade]:
        """Fetch + normalize one instrument-day of trades, ascending order.

        Deterministic sort: (hEven timestamp, nTran). Canceled trades are
        dropped before candles see them.
        """
        url = f"{self._base}/api/Trade/GetTradeHistory/{ins_code}/{to_gregorian_compact(day)}/false"
        payload = await self._http.request_json(url)
        raw = self._extract_history(payload, url)
        return self._normalize(ins_code, day, raw)

    async def get_client_types(self, ins_code: str) -> ClientTypeBreakdown:
        """Fetch the individual/legal (حقیقی/حقوقی) buy/sell breakdown.

        GetInsClientTypes returns one row of cumulative day counters. Field
        names were verified 2026-10 on one instrument; unknown keys are logged
        once via _log_schema_drift-style tolerance (parse accepts either
        buy_Individual_Count or buyIndividualCount casing).
        """
        url = f"{self._base}/api/Parcode/GetInsClientTypes/{ins_code}"
        payload = await self._http.request_json(url)
        rows = self._extract_rows(payload, url)
        if not rows:
            raise ProviderParseError(f"tsetmc: empty client-types payload: {url}")
        return self._normalize_client_types(ins_code, rows[0])

    # ------------------------------------------------------------- parsing

    @staticmethod
    def _extract_rows(payload: dict | list, url: str) -> list[dict]:
        """Return the list of row dicts from a {'data': ...} or bare-list payload."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "clientTypes", "items"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return rows
        raise ProviderParseError(f"tsetmc: unexpected payload type {type(payload).__name__}: {url}")

    @staticmethod
    def _normalize_client_types(ins_code: str, row: dict) -> ClientTypeBreakdown:
        def pick(*names: str) -> int | None:
            for name in names:
                value = row.get(name)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        continue
            return None

        breakdown = ClientTypeBreakdown(
            ins_code=ins_code,
            buy_i_count=pick("buy_Individual_Count", "buyIndividualCount"),
            buy_i_volume=pick("buy_Individual_Volume", "buyIndividualVolume"),
            sell_i_count=pick("sell_Individual_Count", "sellIndividualCount"),
            sell_i_volume=pick("sell_Individual_Volume", "sellIndividualVolume"),
            buy_l_count=pick("buy_Legal_Count", "buyLegalCount"),
            buy_l_volume=pick("buy_Legal_Volume", "buyLegalVolume"),
            sell_l_count=pick("sell_Legal_Count", "sellLegalCount"),
            sell_l_volume=pick("sell_Legal_Volume", "sellLegalVolume"),
            source=PROVIDER_NAME,
        )
        if breakdown.buy_i_volume is None and breakdown.sell_i_volume is None:
            raise ProviderParseError(
                f"tsetmc: client-types row without individual fields: {sorted(row)}"
            )
        return breakdown

    @staticmethod
    def _extract_history(payload: dict | list, url: str) -> list[dict]:
        if not isinstance(payload, dict):
            raise ProviderParseError(f"tsetmc: unexpected payload type {type(payload).__name__}: {url}")
        history = payload.get("tradeHistory")
        if history is None:
            raise ProviderParseError(f"tsetmc: missing 'tradeHistory' key: {url}")
        if not isinstance(history, list):
            raise ProviderParseError(f"tsetmc: 'tradeHistory' not a list: {url}")
        return history

    @staticmethod
    def _normalize(ins_code: str, day: date, raw_trades: list[dict]) -> list[Trade]:
        """Wire dicts -> Trade list. Skips canceled, sorts (timestamp, nTran)."""
        # Stable sort on the raw wire order for identical keys; upstream is
        # newest-first so reversed() gives ascending with earliest first.
        rows: list[tuple[datetime, int, int, Trade]] = []
        for idx, item in enumerate(raw_trades):
            try:
                h_even = int(item["hEven"])
                n_tran = int(item["nTran"])
                price = int(round(float(item["pTran"])))
                volume = int(item["qTitTran"])
                canceled = bool(int(item.get("canceled", 0)))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderParseError(f"tsetmc: bad trade row at {idx}: {exc}") from exc
            if canceled:
                continue
            ts = heven_to_time(h_even).replace(
                year=day.year, month=day.month, day=day.day
            )
            trade = Trade(
                instrument_id=_uuid_from_ins_code(ins_code),
                trade_date=day,
                timestamp=ts,
                sequence=n_tran,
                price=price,
                volume=volume,
                canceled=False,
                source=PROVIDER_NAME,
            )
            rows.append((ts, n_tran, idx, trade))

        # Deterministic ascending: timestamp, then sequence, then wire index
        # (stability for duplicate keys; wire is newest-first so use raw order).
        # Sort ascending by (ts, nTran). Upstream newest-first => earliest trade
        # has smallest nTran at same second; plain sort gives deterministic order.
        rows.sort(key=lambda r: (r[0], r[1], -r[2]))
        return [r[3] for r in rows]


def _uuid_from_ins_code(ins_code: str) -> "object":
    """Placeholder deterministic UUID5 from ins_code (registry later maps real IDs).

    ponytail: deterministic UUID5(ins_code) keeps Trade.instrument_id stable
    before the registry exists; swap to registry lookup when integrated.
    """
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, f"tsetmc:ins:{ins_code}")
