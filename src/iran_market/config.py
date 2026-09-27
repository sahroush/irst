"""Configuration from environment variables. Sensible defaults; no secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

_TEHRAN = ZoneInfo("Asia/Tehran")

DEFAULTS = {
    "TSETMC_BASE_URL": "https://cdn.tsetmc.com",
    "TSE_WEBGW_BASE_URL": "https://webgw.tse.ir",
    "HTTP_TIMEOUT": "30.0",
    "HTTP_CONCURRENCY": "5",
    "HTTP_MAX_RETRIES": "4",
    "HTTP_BACKOFF_BASE": "0.5",
    "HTTP_BACKOFF_CAP": "30.0",
    "MARKET_WATCH_INTERVAL_SECONDS": "15",
    "SCANNER_TIMEFRAME": "30m",
    "TEHRAN_TIMEZONE": "Asia/Tehran",
    "RAW_ARCHIVE_ENABLED": "0",
    "RAW_ARCHIVE_DIR": "data/raw",
    "DATABASE_URL": "sqlite:///data/iran_market.db",
}


def _get(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    tsetmc_base_url: str = DEFAULTS["TSETMC_BASE_URL"]
    tse_webgw_base_url: str = DEFAULTS["TSE_WEBGW_BASE_URL"]
    http_timeout: float = float(DEFAULTS["HTTP_TIMEOUT"])
    http_concurrency: int = int(DEFAULTS["HTTP_CONCURRENCY"])
    http_max_retries: int = int(DEFAULTS["HTTP_MAX_RETRIES"])
    http_backoff_base: float = float(DEFAULTS["HTTP_BACKOFF_BASE"])
    http_backoff_cap: float = float(DEFAULTS["HTTP_BACKOFF_CAP"])
    market_watch_interval_seconds: float = float(DEFAULTS["MARKET_WATCH_INTERVAL_SECONDS"])
    scanner_timeframe: str = DEFAULTS["SCANNER_TIMEFRAME"]
    timezone: ZoneInfo = _TEHRAN
    raw_archive_enabled: bool = False
    raw_archive_dir: str = DEFAULTS["RAW_ARCHIVE_DIR"]
    database_url: str = DEFAULTS["DATABASE_URL"]
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tsetmc_base_url=_get("TSETMC_BASE_URL", DEFAULTS["TSETMC_BASE_URL"]),
            tse_webgw_base_url=_get("TSE_WEBGW_BASE_URL", DEFAULTS["TSE_WEBGW_BASE_URL"]),
            http_timeout=float(_get("HTTP_TIMEOUT", DEFAULTS["HTTP_TIMEOUT"])),
            http_concurrency=int(_get("HTTP_CONCURRENCY", DEFAULTS["HTTP_CONCURRENCY"])),
            http_max_retries=int(_get("HTTP_MAX_RETRIES", DEFAULTS["HTTP_MAX_RETRIES"])),
            http_backoff_base=float(_get("HTTP_BACKOFF_BASE", DEFAULTS["HTTP_BACKOFF_BASE"])),
            http_backoff_cap=float(_get("HTTP_BACKOFF_CAP", DEFAULTS["HTTP_BACKOFF_CAP"])),
            market_watch_interval_seconds=float(
                _get("MARKET_WATCH_INTERVAL_SECONDS", DEFAULTS["MARKET_WATCH_INTERVAL_SECONDS"])
            ),
            scanner_timeframe=_get("SCANNER_TIMEFRAME", DEFAULTS["SCANNER_TIMEFRAME"]),
            raw_archive_enabled=_get("RAW_ARCHIVE_ENABLED", "0").lower() in ("1", "true", "yes"),
            raw_archive_dir=_get("RAW_ARCHIVE_DIR", DEFAULTS["RAW_ARCHIVE_DIR"]),
            database_url=_get("DATABASE_URL", DEFAULTS["DATABASE_URL"]),
            log_level=_get("LOG_LEVEL", "INFO"),
        )

    @classmethod
    def for_tests(cls, **overrides) -> "Settings":
        """Deterministic settings for unit tests (fast retries, no archive)."""
        base = dict(
            http_timeout=5.0,
            http_concurrency=2,
            http_max_retries=2,
            http_backoff_base=0.01,
            http_backoff_cap=0.05,
            raw_archive_enabled=False,
        )
        base.update(overrides)
        return cls(**base)


def default_settings() -> Settings:
    return Settings.from_env()
