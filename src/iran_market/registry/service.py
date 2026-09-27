"""Instrument registry service. Sync from providers is kept OUTSIDE (services/)."""

from __future__ import annotations

from ..logging_setup import get_logger
from ..models import Instrument
from ..storage.base import InstrumentRepository

log = get_logger("registry")


class InstrumentRegistry:
    """Thin domain facade over InstrumentRepository.

    Symbol is NOT assumed unique: get_by_symbol returns a list.
    """

    def __init__(self, repo: InstrumentRepository) -> None:
        self._repo = repo

    def upsert(self, instrument: Instrument) -> Instrument:
        saved = self._repo.upsert(instrument)
        log.debug("instrument upserted", extra={"ins_code": saved.ins_code, "symbol": saved.symbol})
        return saved

    def resolve_by_ins_code(self, ins_code: str) -> Instrument | None:
        return self._repo.get_by_ins_code(ins_code)

    def resolve_by_isin(self, isin: str) -> Instrument | None:
        return self._repo.get_by_isin(isin)

    def resolve_symbol(self, symbol: str) -> list[Instrument]:
        return self._repo.get_by_symbol(symbol)

    def active(self, market: str | None = None, instrument_type: str | None = None) -> list[Instrument]:
        return self._repo.list_active(market=market, instrument_type=instrument_type)

    def set_active(self, instrument_id, active: bool) -> None:
        self._repo.set_active(instrument_id, active)
