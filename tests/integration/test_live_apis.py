"""Live integration tests. Run manually: pytest -m integration -v"""

from datetime import date

import pytest

from iran_market.candles.builder import CandleBuilder
from iran_market.candles.models import Timeframe
from iran_market.config import Settings
from iran_market.sessions import SessionConfig, TradingCalendar

DAY = date(2026, 9, 23)
INS_CODE = "46348559193224090"


@pytest.fixture
def settings():
    return Settings.for_tests()


@pytest.mark.integration
async def test_live_tsetmc_trades_and_candles(settings):
    """Real endpoint: ~21k trades expected for 46348559193224090 on 2026-09-23."""
    from iran_market.http import HttpClient
    from iran_market.providers.tsetmc import TsetmcProvider

    http = HttpClient(settings, provider_name="tsetmc")
    provider = TsetmcProvider(http, "https://cdn.tsetmc.com")
    try:
        trades = await provider.get_trades(INS_CODE, DAY)
    finally:
        await http.aclose()

    print(f"\nInstrument: {INS_CODE}")
    print(f"Date: {DAY.isoformat()}")
    print(f"Trades: {len(trades)}")  # ~21174, don't assert exact
    assert len(trades) > 20000
    first, last = trades[0], trades[-1]
    print(f"First trade: ts={first.timestamp.isoformat()} seq={first.sequence} price={first.price} vol={first.volume}")
    print(f"Last trade:  ts={last.timestamp.isoformat()} seq={last.sequence} price={last.price} vol={last.volume}")

    # deterministic ascending
    keys = [(t.timestamp, t.sequence) for t in trades]
    assert keys == sorted(keys), "trades not ascending by (ts, nTran)"
    assert all(not t.canceled for t in trades)

    # 1h session-anchored candles
    cal = TradingCalendar(SessionConfig())
    edges = cal.hour_boundaries(DAY, 1)
    candles = CandleBuilder().build(trades, edges, Timeframe("1h"))
    print(f"Candles ({len(candles)}):")
    for c in candles:
        print(f"  {c.start_time.strftime('%H:%M')}-{c.end_time.strftime('%H:%M')} "
              f"O={c.open} H={c.high} L={c.low} C={c.close} V={c.volume} N={c.trade_count}")
        assert c.low <= c.open <= c.high
        assert c.low <= c.close <= c.high
        assert c.volume >= 0
    starts = [c.start_time for c in candles]
    assert starts == sorted(starts)

    # first chronological trade determines open of first candle,
    # last chronological trade determines close of its candle
    assert candles[0].open == first.price
    matching_last = [c for c in candles if c.start_time <= last.timestamp < c.end_time]
    assert matching_last and matching_last[0].close == last.price


@pytest.mark.integration
async def test_live_webgw_market_watch(settings):
    from iran_market.http import HttpClient
    from iran_market.providers.tse_webgw import TseWebGwProvider

    http = HttpClient(settings, provider_name="tse_webgw")
    provider = TseWebGwProvider(http, "https://webgw.tse.ir")
    try:
        snaps = await provider.get_market_snapshot()
    finally:
        await http.aclose()
    print(f"\nwebgw items: {len(snaps)}")  # ~586
    assert len(snaps) > 500
    foliad = [s for s in snaps if s.symbol == "فولاد"]
    assert foliad, "فولاد should appear in cash market watch"
    s = foliad[0]
    print(f"فولاد: last={s.last_price} close={s.closing_price} yest={s.yesterday_price} "
          f"vol={s.cumulative_volume} n={s.trade_count}")
    assert s.last_price and s.closing_price
