"""Session config, webgw unwrapping, events."""

from datetime import date

from iran_market.events import EventBus
from iran_market.providers.tse_webgw import to_int, to_str, unwrap
from iran_market.sessions import SessionConfig, TradingCalendar


def test_calendar_holiday_override():
    cfg = SessionConfig(holidays={"2026-09-23": "test holiday"})
    cal = TradingCalendar(cfg)
    assert cal.sessions(date(2026, 9, 23)) == []
    assert cal.is_trading_day(date(2026, 9, 23)) is False


def test_calendar_custom_session_hours():
    cfg = SessionConfig.from_dict({"weekdays": {"5": [["09:00", "14:00"]], "0": [["09:00", "12:30"]]}})
    cal = TradingCalendar(cfg)
    edges = cal.hour_boundaries(date(2026, 9, 26), 1)  # Saturday
    assert (edges[0][0].strftime("%H:%M"), edges[-1][1].strftime("%H:%M")) == ("09:00", "14:00")
    assert len(edges) == 5  # 09-10,10-11,11-12,12-13,13-14


def test_unwrap_value_state():
    assert unwrap({"value": 7252313, "state": 0}) == 7252313
    assert unwrap({"value": None, "state": 0}) is None
    assert unwrap(123) == 123
    assert unwrap(None) is None
    assert to_int({"value": "35030"}) == 35030
    assert to_int({"value": None}) is None
    assert to_str({"value": "iro1x"}) == "iro1x"


def test_event_bus_pub_sub():
    bus = EventBus()
    seen = []
    bus.subscribe("CandleClosed", lambda e, p: seen.append((e, p["candle"])))
    bus.publish("CandleClosed", {"candle": 42})
    bus.publish("CandleUpdated", {"candle": 1})  # no subscriber
    assert seen == [("CandleClosed", 42)]


def test_event_bus_handler_error_isolated():
    bus = EventBus()

    def bad(e, p):
        raise RuntimeError("consumer bug")

    seen = []
    bus.subscribe("X", bad)
    bus.subscribe("X", lambda e, p: seen.append(1))
    bus.publish("X", {})
    assert seen == [1]  # second handler unaffected


def test_registry_sync_uses_public_webgw_api(tmp_path):
    """Regression: sync must not reach for provider.MARKET_WATCH_PATH internals."""
    import asyncio
    from iran_market.providers.tsetmc_legacy import LegacyInstrumentRow
    from iran_market.registry.sync import RegistrySyncService
    from iran_market.storage.sqlite import SqliteInstrumentRepository, SqliteStore, create_schema

    class WebGw:
        async def get_market_watch_items(self):
            return [{
                "instrumentId": "IRO1FOLD0001",
                "instrumentName": "فولاد",
                "companyNamePersian": "فولاد مبارکه اصفهان",
                "marketname": "بورس",
                "securitiesname": "سهام عادی",
                "statename": "مجاز",
                "marketid": 1,
            }]

    class Legacy:
        async def fetch_instrument_list(self):
            return [LegacyInstrumentRow("46348559193224090", "IRO1FOLD0001", "فولاد", "فولاد مبارکه اصفهان")]

    store = SqliteStore(tmp_path / "registry.sqlite")
    create_schema(store)
    repo = SqliteInstrumentRepository(store)
    report = asyncio.run(RegistrySyncService(WebGw(), Legacy(), repo).sync())
    inst = repo.get_by_isin("IRO1FOLD0001")
    assert report.total == 1
    assert inst is not None
    assert inst.ins_code == "46348559193224090"
