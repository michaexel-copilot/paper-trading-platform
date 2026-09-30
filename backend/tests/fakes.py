"""Test doubles: a scriptable price source and a clock the test controls."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.base import Bar, Instrument, NotListed, Quote, SourceError

# A Wednesday, while New York, the forex market and CME futures are all open.
START = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)


class Clock:
    """Wall clock and monotonic clock that move only when the test says so."""

    def __init__(self, start: datetime = START):
        self.current = start
        self.elapsed = 0.0

    def __call__(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds

    def set(self, moment: datetime) -> None:
        self.advance((moment - self.current).total_seconds())


def dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class FakeSource:
    """A MarketDataSource whose answers the test scripts."""

    def __init__(self, name: str, clock: Clock | None = None):
        self.name = name
        self.clock = clock or Clock()
        self.instruments: dict[str, Instrument] = {}
        self.quotes: dict[str, Quote] = {}
        self.bars: dict[tuple[str, str], list[Bar]] = {}
        self.calls: Counter[str] = Counter()
        self.failing = False
        self.delay = 0.0

    # --- scripting -----------------------------------------------------------

    def add(
        self,
        symbol: str,
        instrument_type: str = "crypto",
        currency: str = "USD",
        *,
        name: str | None = None,
        exchange: str | None = None,
        step=None,
        minimum=None,
        base: str | None = None,
    ) -> Instrument:
        instrument = Instrument(
            symbol=symbol,
            name=name or symbol,
            instrument_type=instrument_type,
            quote_currency=currency,
            amount_step=dec(step),
            min_amount=dec(minimum),
            exchange=exchange,
            base=base,
        )
        self.instruments[symbol] = instrument
        return instrument

    def price(
        self,
        symbol: str,
        last,
        bid=None,
        ask=None,
        currency: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Set the quote. Without ``observed_at`` the quote is as of each fetch."""
        if symbol not in self.instruments:
            self.add(symbol, currency=currency or "USD")
        self.quotes[symbol] = Quote(
            last=dec(last),
            bid=dec(bid),
            ask=dec(ask),
            currency=currency or self.instruments[symbol].quote_currency,
            observed_at=observed_at,
            source=self.name,
        )

    # --- MarketDataSource ------------------------------------------------------

    async def _enter(self, method: str) -> None:
        self.calls[method] += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failing:
            raise SourceError(f"{self.name} is down")

    async def load_markets(self) -> dict[str, Instrument]:
        await self._enter("load_markets")
        return dict(self.instruments)

    async def instrument(self, symbol: str) -> Instrument:
        await self._enter("instrument")
        if symbol not in self.instruments:
            raise NotListed(symbol)
        return self.instruments[symbol]

    async def fetch_ticker(self, symbol: str) -> Quote:
        await self._enter("fetch_ticker")
        if symbol not in self.quotes:
            raise NotListed(symbol)
        quote = self.quotes[symbol]
        if quote.observed_at is None:
            return Quote(
                quote.last, quote.bid, quote.ask, quote.currency, self.clock(), quote.source
            )
        return quote

    async def fetch_ohlcv(self, symbol, timeframe, since, limit=None) -> list[Bar]:
        await self._enter("fetch_ohlcv")
        if symbol not in self.instruments:
            raise NotListed(symbol)
        bars = [bar for bar in self.bars.get((symbol, timeframe), []) if bar.time >= since]
        return bars[:limit] if limit else bars

    async def search(self, query: str) -> list[Instrument]:
        await self._enter("search")
        needle = query.lower()
        return [
            i
            for i in self.instruments.values()
            if needle in i.symbol.lower() or needle in i.name.lower()
        ]

    async def close(self) -> None:
        self.calls["close"] += 1


def daily_bars(start: datetime, closes: list) -> list[Bar]:
    return [
        Bar(
            time=start + timedelta(days=offset),
            open=dec(close),
            high=dec(close),
            low=dec(close),
            close=dec(close),
            volume=dec(1000),
        )
        for offset, close in enumerate(closes)
    ]
