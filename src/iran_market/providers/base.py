"""Provider abstraction. Providers normalize wire formats into domain models.

Application code never constructs provider URLs; all provider-specific
parsing lives under ``providers/``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import AsyncIterator

from ..models import Instrument, MarketSnapshot, Trade

PROVIDER_CAPABILITIES = (
    "instruments",      # instrument discovery
    "market_snapshot",  # bulk market watch
    "trades",           # trade history (exact candles)
    "daily_history",    # OHLCV daily bars
    "market_status",    # open/closed/holiday state
)


class MarketDataProvider(ABC):
    """Abstract market-data provider with capability-based discovery."""

    name: str = "abstract"

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities()

    # ------------------------------------------------------------- optional

    async def get_instruments(self) -> AsyncIterator[Instrument]:
        raise ProviderNotSupportedImpl(self.name, "get_instruments")
        yield  # pragma: no cover

    async def get_market_snapshot(self, ins_codes: list[str] | None = None) -> list[MarketSnapshot]:
        raise ProviderNotSupportedImpl(self.name, "get_market_snapshot")

    async def get_trades(self, ins_code: str, day: date) -> list[Trade]:
        raise ProviderNotSupportedImpl(self.name, "get_trades")

    async def get_daily_history(self, ins_code: str, start: date, end: date) -> list[dict]:
        raise ProviderNotSupportedImpl(self.name, "get_daily_history")

    async def get_market_status(self) -> dict:
        raise ProviderNotSupportedImpl(self.name, "get_market_status")


class ProviderNotSupportedImpl(NotImplementedError):
    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(f"{provider} does not implement {capability}")


def not_supported(provider: str, capability: str) -> Exception:
    return ProviderNotSupportedImpl(provider, capability)
