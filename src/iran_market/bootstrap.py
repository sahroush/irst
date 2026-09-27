"""Dependency wiring: build providers/repos/services from Settings. No globals."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .http import HttpClient
from .logging_setup import setup_logging
from .providers.tsetmc import TsetmcProvider
from .providers.tsetmc_legacy import TsetmcLegacyProvider
from .events import EventBus
from .providers.tse_webgw import TseWebGwProvider
from .sessions import SessionConfig, TradingCalendar
from .storage.archive import RawArchive, open_archive
from .storage.sqlite import (
    SqliteCandleRepository,
    SqliteFetchRunRepository,
    SqliteInstrumentRepository,
    SqliteLiveStateRepository,
    SqliteSignalRepository,
    SqliteStore,
    SqliteTradeRepository,
    create_schema,
)


@dataclass
class Container:
    """Wired dependencies for one process. Construct via :func:`build`."""

    settings: Settings
    http_tsetmc: HttpClient
    http_webgw: HttpClient
    tsetmc: TsetmcProvider
    webgw: TseWebGwProvider
    legacy: TsetmcLegacyProvider
    store: SqliteStore
    instruments_repo: SqliteInstrumentRepository
    trades_repo: SqliteTradeRepository
    candles_repo: SqliteCandleRepository
    fetch_repo: SqliteFetchRunRepository
    live_state_repo: SqliteLiveStateRepository
    signals_repo: SqliteSignalRepository
    events: EventBus
    archive: RawArchive
    calendar: TradingCalendar

    async def aclose(self) -> None:
        await self.http_tsetmc.aclose()
        await self.http_webgw.aclose()


def build(settings: Settings | None = None, calendar: TradingCalendar | None = None) -> Container:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_level)

    http_tsetmc = HttpClient(settings, provider_name="tsetmc")
    http_webgw = HttpClient(settings, provider_name="tse_webgw")
    tsetmc = TsetmcProvider(http_tsetmc, settings.tsetmc_base_url)
    webgw = TseWebGwProvider(http_webgw, settings.tse_webgw_base_url)
    legacy = TsetmcLegacyProvider(http_tsetmc)
    events = EventBus()

    store = SqliteStore(settings.database_url)
    create_schema(store)

    return Container(
        settings=settings,
        http_tsetmc=http_tsetmc,
        http_webgw=http_webgw,
        tsetmc=tsetmc,
        webgw=webgw,
        legacy=legacy,
        store=store,
        instruments_repo=SqliteInstrumentRepository(store),
        trades_repo=SqliteTradeRepository(store),
        candles_repo=SqliteCandleRepository(store),
        fetch_repo=SqliteFetchRunRepository(store),
        live_state_repo=SqliteLiveStateRepository(store),
        signals_repo=SqliteSignalRepository(store),
        events=events,
        archive=open_archive(settings),
        calendar=calendar or TradingCalendar(SessionConfig()),
    )
