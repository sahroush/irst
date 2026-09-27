"""Error hierarchy for providers, normalization, storage, candles."""

from __future__ import annotations


class IranMarketError(Exception):
    """Base class for all toolkit errors."""


# ---------------------------------------------------------------- providers


class ProviderError(IranMarketError):
    """Base class for provider-side failures (network, HTTP, parse, block)."""


class ProviderTimeoutError(ProviderError):
    """Request exceeded the configured timeout."""


class ProviderRateLimitError(ProviderError):
    """Server signals rate limiting (HTTP 429)."""


class ProviderBlockedError(ProviderError):
    """Server returned an HTML/block/error page instead of expected JSON."""


class ProviderParseError(ProviderError):
    """Response was 200-JSON-ish but not the expected schema, or malformed JSON."""


class ProviderNotSupported(ProviderError):
    """Provider does not implement the requested capability."""


# ------------------------------------------------------------- domain/data


class DataValidationError(IranMarketError):
    """Domain data failed normalization or consistency validation."""


class RegistryError(IranMarketError):
    """Instrument registry constraint violated (e.g. duplicate natural key)."""


class StorageError(IranMarketError):
    """Persistence-layer failure."""
