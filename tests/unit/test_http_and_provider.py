"""HTTP behavior: timeout, retries, 5xx, HTML/block detection, malformed JSON.

FakeHttpClient patches HttpClient.request_json paths via a respx-free fake
httpx transport.
"""

from datetime import date

import httpx
import pytest

from iran_market.errors import (
    ProviderBlockedError,
    ProviderParseError,
    ProviderTimeoutError,
)
from iran_market.http import HttpClient
from iran_market.models import Candle, CandleStatus
from iran_market.providers.tsetmc import TsetmcProvider
from iran_market.config import Settings

from .conftest import INS_CODE, tsetmc_row


def make_client(handler, settings=None) -> HttpClient:
    s = settings or Settings.for_tests()
    client = HttpClient(s, provider_name="test")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=s.http_timeout,
        headers={"User-Agent": s.user_agent},
    )
    return client


async def test_success_json(settings):
    payload = {"tradeHistory": [tsetmc_row(100000, 1)]}

    def handler(request):
        return httpx.Response(200, json=payload, headers={"content-type": "application/json"})

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://cdn.tsetmc.com")
    trades = await provider.get_trades(INS_CODE, date(2026, 9, 23))
    assert len(trades) == 1


async def test_timeout_raises_and_retries(settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("too slow", request=request)

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://x")
    with pytest.raises(ProviderTimeoutError):
        await provider.get_trades(INS_CODE, date(2026, 9, 23))
    assert calls["n"] == settings.http_max_retries + 1  # initial + retries


async def test_http_5xx_retries_then_raises(settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="busy")

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://x")
    with pytest.raises(Exception):
        await provider.get_trades(INS_CODE, date(2026, 9, 23))
    assert calls["n"] == settings.http_max_retries + 1


async def test_http_4xx_no_retry(settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://x")
    with pytest.raises(Exception) as ei:
        await provider.get_trades(INS_CODE, date(2026, 9, 23))
    assert calls["n"] == 1  # no retry for client errors
    assert "404" in str(ei.value)


async def test_html_block_response(settings):
    def handler(request):
        return httpx.Response(200, text="<html><body>Access Denied</body></html>",
                              headers={"content-type": "text/html"})

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://x")
    with pytest.raises(ProviderBlockedError):
        await provider.get_trades(INS_CODE, date(2026, 9, 23))
    # cooldown engaged: next request fails fast with ProviderBlockedError
    with pytest.raises(ProviderBlockedError):
        await provider.get_trades(INS_CODE, date(2026, 9, 23))


async def test_malformed_json(settings):
    def handler(request):
        return httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"})

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://x")
    with pytest.raises(ProviderParseError):
        await provider.get_trades(INS_CODE, date(2026, 9, 23))


async def test_empty_trade_history(settings):
    def handler(request):
        return httpx.Response(200, json={"tradeHistory": []}, headers={"content-type": "application/json"})

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://x")
    trades = await provider.get_trades(INS_CODE, date(2026, 9, 23))
    assert trades == []


async def test_missing_trade_history_key(settings):
    def handler(request):
        return httpx.Response(200, json={"something": "else"}, headers={"content-type": "application/json"})

    c = make_client(handler, settings)
    provider = TsetmcProvider(c, "https://x")
    with pytest.raises(ProviderParseError):
        await provider.get_trades(INS_CODE, date(2026, 9, 23))
