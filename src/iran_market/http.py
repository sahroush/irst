"""Shared async HTTP machinery for providers.

Responsibilities: bounded concurrency, timeout, retries with exponential
backoff + jitter, error classification (timeout / rate-limit / block / parse),
provider-level cooldown after suspected soft blocks. Providers never build
their own HTTP session; they call :class:`HttpClient.request_json`.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .config import Settings
from .errors import (
    ProviderBlockedError,
    ProviderError,
    ProviderParseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from .logging_setup import get_logger

log = get_logger("http")

RETRYABLE_STATUS = {500, 502, 503, 504}


@dataclass
class HttpResult:
    status_code: int
    body: bytes
    content_type: str


@dataclass
class HttpClient:
    """Bounded-concurrency, retrying, block-aware JSON HTTP client.

    One instance per provider (owns its cooldown state). Configure via
    :class:`~iran_market.config.Settings`.
    """

    settings: Settings
    provider_name: str = "http"
    cooldown_until: datetime | None = None
    _semaphore: asyncio.Semaphore | None = None
    _client: httpx.AsyncClient | None = None

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.settings.http_concurrency)
        self._client = httpx.AsyncClient(
            timeout=self.settings.http_timeout,
            headers={"User-Agent": self.settings.user_agent},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request_raw(self, url: str, *, method: str = "GET") -> HttpResult:
        """GET raw bytes with the same retry/cooldown/error classification as
        request_json, but no JSON parsing or block detection. For non-JSON
        legacy endpoints (gzip text, etc.)."""
        if self.cooldown_until is not None:
            remaining = (self.cooldown_until - _utcnow()).total_seconds()
            if remaining > 0:
                raise ProviderBlockedError(
                    f"{self.provider_name} in cooldown for {remaining:.1f}s"
                )
        attempt = 0
        last_error: Exception | None = None
        max_retries = self.settings.http_max_retries
        base = self.settings.http_backoff_base
        cap = self.settings.http_backoff_cap
        while attempt <= max_retries:
            attempt += 1
            try:
                async with self._semaphore:
                    response = await self._client.request(method, url)
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeoutError(f"{self.provider_name} timeout: {exc}")
            except httpx.HTTPError as exc:
                last_error = ProviderError(f"{self.provider_name} network error: {exc}")
            else:
                if response.status_code == 429:
                    last_error = ProviderRateLimitError(f"{self.provider_name} rate limited (429)")
                elif response.status_code in RETRYABLE_STATUS:
                    last_error = ProviderError(f"{self.provider_name} server error {response.status_code}")
                elif response.status_code >= 400:
                    raise ProviderError(f"{self.provider_name} HTTP {response.status_code}: {url}")
                else:
                    return HttpResult(
                        status_code=response.status_code,
                        body=response.content,
                        content_type=response.headers.get("content-type", ""),
                    )
            if attempt > max_retries:
                raise last_error if last_error else ProviderError("unreachable")  # pragma: no cover
            delay = min(cap, base * (2 ** (attempt - 1))) * random.uniform(0.5, 1.5)
            await asyncio.sleep(delay)
        raise last_error if last_error else ProviderError("unreachable")  # pragma: no cover

    async def request_json(self, url: str, *, method: str = "GET") -> dict | list:
        """GET (or other method) expecting a JSON body.

        Raises ProviderTimeoutError / ProviderRateLimitError /
        ProviderBlockedError / ProviderParseError. Retries transient failures
        with exponential backoff + jitter. Engages a provider-level cooldown
        on suspected soft blocks.
        """
        if self.cooldown_until is not None:
            remaining = (self.cooldown_until - _utcnow()).total_seconds()
            if remaining > 0:
                log.warning(
                    "provider cooling down, skipping request",
                    extra={"provider": self.provider_name, "seconds": round(remaining, 1), "url": url},
                )
                raise ProviderBlockedError(
                    f"{self.provider_name} in cooldown for {remaining:.1f}s"
                )

        attempt = 0
        last_error: Exception | None = None
        max_retries = self.settings.http_max_retries
        base = self.settings.http_backoff_base
        cap = self.settings.http_backoff_cap

        while attempt <= max_retries:
            attempt += 1
            started = time.monotonic()
            try:
                async with self._semaphore:
                    response = await self._client.request(method, url)
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeoutError(f"{self.provider_name} timeout: {exc}")
                log.warning("timeout", extra={"provider": self.provider_name, "url": url, "attempt": attempt})
            except httpx.HTTPError as exc:
                last_error = ProviderError(f"{self.provider_name} network error: {exc}")
                log.warning("network error", extra={"provider": self.provider_name, "url": url, "attempt": attempt})
            else:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                status = response.status_code
                log.debug(
                    "http response",
                    extra={"provider": self.provider_name, "status": status, "ms": elapsed_ms, "url": url},
                )
                if status == 429:
                    last_error = ProviderRateLimitError(f"{self.provider_name} rate limited (429)")
                elif status in RETRYABLE_STATUS:
                    last_error = ProviderError(f"{self.provider_name} server error {status}")
                elif status >= 400:
                    # Non-retryable client error.
                    raise ProviderError(f"{self.provider_name} HTTP {status}: {url}", )
                else:
                    return self._parse_body(response)
                # fallthrough: retryable error -> continue loop

            if attempt > max_retries:
                assert last_error is not None
                raise last_error
            delay = min(cap, base * (2 ** (attempt - 1)))
            delay *= random.uniform(0.5, 1.5)  # jitter
            log.info("retrying", extra={"provider": self.provider_name, "attempt": attempt, "delay_s": round(delay, 2)})
            await asyncio.sleep(delay)

        raise last_error if last_error else ProviderError("unreachable")  # pragma: no cover

    def _parse_body(self, response: httpx.Response) -> dict | list:
        """Content-type/body checks, JSON decode, block detection."""
        content_type = response.headers.get("content-type", "")
        body = response.content
        text_head = body[:2048].decode(response.encoding or "utf-8", errors="replace").lstrip().lower()

        if "json" not in content_type and not (text_head.startswith("{") or text_head.startswith("[")):
            self._engage_cooldown("content-type mismatch")
            raise ProviderBlockedError(
                f"{self.provider_name}: non-JSON response (ct={content_type!r}, status={response.status_code})"
            )
        if "<html" in text_head or "<!doctype" in text_head:
            self._engage_cooldown("HTML response")
            raise ProviderBlockedError(f"{self.provider_name}: HTML block/error page")

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderParseError(f"{self.provider_name}: malformed JSON: {exc}") from exc

        if isinstance(data, dict):
            for marker in ("error", "message"):
                v = data.get(marker)
                if isinstance(v, str) and any(
                    w in v.lower() for w in ("blocked", "access denied", "forbidden", "محروم", "مسدود")
                ):
                    self._engage_cooldown("block message in JSON")
                    raise ProviderBlockedError(f"{self.provider_name}: block message: {v}")
        if not (isinstance(data, dict) or isinstance(data, list)):
            raise ProviderParseError(f"{self.provider_name}: unexpected JSON type {type(data).__name__}")
        return data

    def _engage_cooldown(self, reason: str) -> None:
        """Back off this provider for a few minutes after suspected blocking."""
        cooldown_s = 120.0
        self.cooldown_until = _utcnow() + _timedelta(seconds=cooldown_s)
        log.warning(
            "engaging provider cooldown",
            extra={"provider": self.provider_name, "reason": reason, "seconds": cooldown_s},
        )


def _utcnow() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)


def _timedelta(seconds: float):
    from datetime import timedelta
    return timedelta(seconds=seconds)
