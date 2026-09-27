"""TSETMC old-portal tsev2 endpoints: bulk instrument list (InsCode↔ISIN).

Verified behavior (2026-09-26):
- GET {base}/tsev2/data/MarketWatchInit.aspx?h=0&r=0 returns gzip body of
  ';'-separated rows. Row 0 is an index row (44 comma fields); instrument rows
  have 26 comma fields:
    0 insCode  1 ISIN  2 lVal18 (short)  3 lVal30 (long Persian name)
    4 hEven    5 price  6 close  7 yesterday  8 count / 9 volume ...
- ~22k rows total; ~642 IRO1* cash-equity rows. Old portal may vanish;
  failure here degrades gracefully (registry keeps working without InsCode).
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass

from ..errors import ProviderError, ProviderParseError
from ..http import HttpClient
from ..logging_setup import get_logger

log = get_logger("tsetmc_legacy")

MARKET_WATCH_INIT_PATH = "/tsev2/data/MarketWatchInit.aspx?h=0&r=0"


@dataclass(frozen=True)
class LegacyInstrumentRow:
    ins_code: str
    isin: str
    symbol: str  # lVal18
    name: str  # lVal30


class TsetmcLegacyProvider:
    """Old tsev2 endpoints — currently the only bulk InsCode↔ISIN source found."""

    name = "tsetmc_legacy"

    def __init__(self, http: HttpClient, base_url: str = "https://old.tsetmc.com") -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def fetch_instrument_list(self) -> list[LegacyInstrumentRow]:
        """Bulk instrument list. Returns [] on parse failure (mapping is optional)."""
        url = f"{self._base}{MARKET_WATCH_INIT_PATH}"
        try:
            response = await self._http.request_raw(url)
        except ProviderError as exc:
            log.warning("legacy instrument list unavailable: %s", exc)
            return []

        body = response.body
        try:
            text = gzip.decompress(body).decode("utf-8", errors="replace")
        except (OSError, gzip.BadGzipFile):
            text = body.decode("utf-8", errors="replace")

        rows: list[LegacyInstrumentRow] = []
        for raw_row in text.split(";"):
            if not raw_row.strip():
                continue
            fields = raw_row.split(",")
            if len(fields) < 4 or not fields[0].strip().isdigit():
                continue  # index row / header junk
            ins_code, isin, symbol, name = (f.strip() for f in fields[:4])
            if not isin:
                continue
            rows.append(LegacyInstrumentRow(ins_code=ins_code, isin=isin, symbol=symbol, name=name))
        if not rows:
            raise ProviderParseError("tsetmc_legacy: instrument list parsed to zero rows")
        log.info("legacy instrument list parsed", extra={"rows": len(rows)})
        return rows
