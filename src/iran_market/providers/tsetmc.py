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
from datetime import date, datetime, timedelta

from ..errors import ProviderParseError
from ..http import HttpClient
from ..logging_setup import get_logger
from ..models import Candle, CandleStatus, ClientTypeBreakdown, Trade
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
        return frozenset({"trades", "client_types", "daily_closings"})

    async def get_trades(self, ins_code: str, day: date) -> list[Trade]:
        """Fetch + normalize one instrument-day of trades, ascending order.

        Deterministic sort: (hEven timestamp, nTran). Canceled trades are
        dropped before candles see them.
        """
        url = f"{self._base}/api/Trade/GetTradeHistory/{ins_code}/{to_gregorian_compact(day)}/false"
        payload = await self._http.request_json(url)
        raw = self._extract_history(payload, url)
        return self._normalize(ins_code, day, raw)

    async def get_daily_closings(self, ins_code: str, instrument_id, days: int = 130) -> list[Candle]:
        """End-of-day OHLCV bars for one instrument, oldest-first.

        GetClosingPriceDailyList returns the exchange's official daily bars
        (priceFirst/priceMax/priceMin/pClosing, qTotTran5J volume, zTotTran
        trade count), newest-first. Verified live 2026-10-17: high/low/volume/
        trade_count match reconciled 30m aggregates exactly. Bars are stamped
        at the 09:00 session open because EOD rows carry no time; charts and
        the EMA screens read them fine, but they must never be used for
        intraday flip replay. Zero-trade days (no price discovery) are
        dropped. ponytail: pClosing vs pDrCotVal (adjusted close) — we use
        pClosing, matching the reconciled close; revisit if capital changes
        (تقسیم/افزایش سرمایه) start distorting history.
        """
        url = f"{self._base}/api/ClosingPrice/GetClosingPriceDailyList/{ins_code}/{days}"
        payload = await self._http.request_json(url)
        rows = self._extract_rows(payload, url)
        bars: list[Candle] = []
        for row in rows:
            if not row.get("qTotTran5J"):
                continue  # no trades: not a real session bar
            d_even = int(row.get("dEven") or 0)
            if not d_even:
                continue
            stamp = str(d_even).zfill(8)
            start = datetime(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]), 9, 0, tzinfo=TEHRAN)
            bars.append(Candle(
                instrument_id=instrument_id,
                timeframe="1d", start_time=start, end_time=start + timedelta(hours=4),
                open=int(round(float(row.get("priceFirst") or 0))),
                high=int(round(float(row.get("priceMax") or 0))),
                low=int(round(float(row.get("priceMin") or 0))),
                close=int(round(float(row.get("pClosing") or 0))),
                volume=int(round(float(row.get("qTotTran5J") or 0))),
                trade_count=int(round(float(row.get("zTotTran") or 0))),
                status=CandleStatus.RECONCILED, source="eod",
            ))
        bars.reverse()
        return bars

    async def get_client_types_all(self) -> list[ClientTypeBreakdown]:
        """Fetch the whole market's individual/legal (حقیقی/حقوقی) split in one call.

        GetClientTypeAll returns one row per instrument with cumulative day
        counters (I = individual, N = legal). Endpoint + field names verified
        live 2026-10-17 against the payload itself; the per-instrument
        Parcode/GetInsClientTypes path used earlier does not exist.
        """
        url = f"{self._base}/api/ClientType/GetClientTypeAll"
        payload = await self._http.request_json(url)
        rows = self._extract_rows(payload, url)
        return [self._normalize_client_types(row) for row in rows]

    # ------------------------------------------------------------- parsing

    @staticmethod
    def _extract_rows(payload: dict | list, url: str) -> list[dict]:
        """Return the list of row dicts from a {'data': ...} or bare-list payload."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "closingPriceDaily", "clientTypes", "clientTypeAllDto", "clientType", "items"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return rows
        raise ProviderParseError(f"tsetmc: unexpected payload type {type(payload).__name__}: {url}")

    @staticmethod
    def _normalize_client_types(row: dict) -> ClientTypeBreakdown:
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
            ins_code=str(row.get("insCode") or ""),
            buy_i_count=pick("buy_CountI", "buy_I_Count"),
            buy_i_volume=pick("buy_I_Volume"),
            sell_i_count=pick("sell_CountI", "sell_I_Count"),
            sell_i_volume=pick("sell_I_Volume"),
            buy_l_count=pick("buy_CountN", "buy_N_Count"),
            buy_l_volume=pick("buy_N_Volume"),
            sell_l_count=pick("sell_CountN", "sell_N_Count"),
            sell_l_volume=pick("sell_N_Volume"),
            source=PROVIDER_NAME,
        )
        if not breakdown.ins_code:
            raise ProviderParseError(f"tsetmc: client-types row without insCode: {sorted(row)}")
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
