import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from app.marketdata.base import (
    Bar,
    DataUnavailable,
    Instrument,
    MarketDataSource,
    NotListed,
    Quote,
)

log = logging.getLogger(__name__)

FAILURES_TO_TRIP = 3
TRIP_SECONDS = 60.0
SOURCE_TIMEOUT_SECONDS = 25.0


@dataclass(frozen=True)
class AssetRef:
    """What the router needs to know about an asset: its class and per-source symbols."""

    key: object
    asset_class: str
    symbols: Mapping[str, str] = field(default_factory=dict)


class SourceRouter:
    """Walks an asset class's source chain until one source answers."""

    def __init__(
        self,
        sources: Mapping[str, MarketDataSource],
        chains: Mapping[str, list[str]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self.sources = dict(sources)
        self.chains = {cls: list(chain) for cls, chain in chains.items()}
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._tripped_until: dict[str, float] = {}

    def candidates(self, ref: AssetRef) -> list[tuple[MarketDataSource, str]]:
        return [
            (self.sources[name], ref.symbols[name])
            for name in self.chains.get(ref.asset_class, [])
            if name in self.sources and name in ref.symbols
        ]

    def is_tripped(self, name: str) -> bool:
        return self._clock() < self._tripped_until.get(name, 0.0)

    def _record_failure(self, name: str, exc: BaseException) -> None:
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        log.warning("source %s failed (%d in a row): %r", name, count, exc)
        if count >= FAILURES_TO_TRIP:
            self._tripped_until[name] = self._clock() + TRIP_SECONDS
            self._failures[name] = 0

    async def _first(self, ref: AssetRef, call):
        """Run ``call(source, symbol)`` down the chain and return the first answer."""
        errors: list[str] = []
        for source, symbol in self.candidates(ref):
            if self.is_tripped(source.name):
                errors.append(f"{source.name}: skipped after repeated failures")
                continue
            try:
                result = await asyncio.wait_for(call(source, symbol), SOURCE_TIMEOUT_SECONDS)
            except NotListed:
                errors.append(f"{source.name}: not listed")
                continue
            except Exception as exc:  # any failure moves on to the next source
                self._record_failure(source.name, exc)
                errors.append(f"{source.name}: failed")
                continue
            self._failures[source.name] = 0
            return source.name, result
        detail = "; ".join(errors) or "no source configured"
        raise DataUnavailable(f"no source could supply data ({detail})")

    async def fetch_quote(self, ref: AssetRef) -> Quote:
        _, quote = await self._first(ref, lambda source, symbol: source.fetch_ticker(symbol))
        return quote

    async def fetch_history(
        self, ref: AssetRef, timeframe: str, since: datetime, limit: int | None = None
    ) -> tuple[str, list[Bar]]:
        return await self._first(
            ref, lambda source, symbol: source.fetch_ohlcv(symbol, timeframe, since, limit)
        )

    async def resolve(self, ref: AssetRef) -> tuple[str, Instrument] | None:
        """The first source in the chain that lists the asset, or None when none does.

        Raises DataUnavailable when a source failed, because the answer is then unknown.
        """
        failed = False
        for source, symbol in self.candidates(ref):
            if self.is_tripped(source.name):
                failed = True
                continue
            try:
                instrument = await asyncio.wait_for(
                    source.instrument(symbol), SOURCE_TIMEOUT_SECONDS
                )
            except NotListed:
                continue
            except Exception as exc:
                self._record_failure(source.name, exc)
                failed = True
                continue
            self._failures[source.name] = 0
            return source.name, instrument
        if failed:
            raise DataUnavailable("a source failed while resolving the asset")
        return None

    async def close(self) -> None:
        for source in self.sources.values():
            try:
                await source.close()
            except Exception:
                log.exception("closing source %s failed", source.name)
