import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

# How long an answer is reused before the source is asked again.
QUOTE_TTL_SECONDS = {"crypto": 5.0}
DEFAULT_QUOTE_TTL_SECONDS = 60.0


def quote_ttl(asset_class: str) -> float:
    return QUOTE_TTL_SECONDS.get(asset_class, DEFAULT_QUOTE_TTL_SECONDS)


class TTLCache:
    """TTL cache in which concurrent callers for one key share a single fetch."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._values: dict[Hashable, tuple[float, Any]] = {}
        self._inflight: dict[Hashable, asyncio.Future] = {}

    def peek(self, key: Hashable) -> Any | None:
        """The last value stored for the key, however old."""
        entry = self._values.get(key)
        return entry[1] if entry else None

    async def get(self, key: Hashable, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
        entry = self._values.get(key)
        if entry and self._clock() - entry[0] < ttl:
            return entry[1]
        pending = self._inflight.get(key)
        if pending is None:
            pending = asyncio.ensure_future(self._fetch(key, fetch))
            self._inflight[key] = pending
        return await asyncio.shield(pending)

    async def _fetch(self, key: Hashable, fetch: Callable[[], Awaitable[Any]]) -> Any:
        try:
            value = await fetch()
            self._values[key] = (self._clock(), value)
            return value
        finally:
            self._inflight.pop(key, None)
