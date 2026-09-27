"""Lightweight in-process event bus. No external broker required."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from .logging_setup import get_logger

log = get_logger("events")

Handler = Callable[[str, dict], None] | Callable[[str, dict], Awaitable[None]]


class EventBus:
    """Sync/async publish-subscribe within one process.

    Handlers must not raise: exceptions are logged and swallowed so one bad
    consumer cannot break the publisher.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    def publish(self, event: str, payload: dict[str, Any]) -> None:
        for handler in list(self._handlers.get(event, [])):
            try:
                result = handler(event, payload)
                if asyncio.iscoroutine(result):
                    # fire-and-forget with exception logging
                    task = asyncio.get_running_loop().create_task(self._run_async(event, result))
                    task.add_done_callback(lambda t: t.exception() and log.error("event handler crashed",
                                     extra={"event": event, "error": str(t.exception())}))
            except Exception:
                log.exception("event handler failed", extra={"event": event})

    @staticmethod
    async def _run_async(event: str, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("async event handler failed", extra={"event": event})
