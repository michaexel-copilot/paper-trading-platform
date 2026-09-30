"""The seam between the platform and its price sources.

The interface follows ccxt's unified API (``load_markets``, ``fetch_ticker``,
``fetch_ohlcv``) so that a ccxt exchange and a non-exchange source such as
Yahoo Finance look the same to the rest of the system.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

TIMEFRAMES = ("1d", "1h")

# Instrument types a source can report, mapped to the platform's asset classes.
ASSET_CLASS_BY_TYPE = {
    "crypto": "crypto",
    "equity": "stocks",
    "etf": "etfs",
    "future": "commodities",
    "currency": "forex",
}


class SourceError(Exception):
    """The source failed to answer."""


class NotListed(SourceError):
    """The source answered and does not list the symbol."""


class DataUnavailable(Exception):
    """No source in the chain could supply the data."""


def to_decimal(value) -> Decimal | None:
    """Convert a source value to Decimal without passing through binary float arithmetic."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    result = Decimal(str(value))
    return result if result.is_finite() else None


@dataclass(frozen=True, slots=True)
class Quote:
    last: Decimal
    bid: Decimal | None
    ask: Decimal | None
    currency: str
    observed_at: datetime
    source: str

    @property
    def last_price_only(self) -> bool:
        return self.bid is None or self.ask is None


@dataclass(frozen=True, slots=True)
class Bar:
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    name: str
    instrument_type: str  # crypto | equity | etf | future | currency | anything else
    quote_currency: str
    amount_step: Decimal | None = None
    min_amount: Decimal | None = None
    maker: Decimal | None = None
    taker: Decimal | None = None
    exchange: str | None = None
    base: str | None = None

    @property
    def asset_class(self) -> str | None:
        return ASSET_CLASS_BY_TYPE.get(self.instrument_type)


@runtime_checkable
class MarketDataSource(Protocol):
    name: str

    async def load_markets(self) -> dict[str, Instrument]:
        """All instruments the source can enumerate, keyed by its own symbol."""

    async def instrument(self, symbol: str) -> Instrument:
        """One instrument. Raises NotListed when the source does not have it."""

    async def fetch_ticker(self, symbol: str) -> Quote: ...

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: datetime, limit: int | None = None
    ) -> list[Bar]:
        """Bars in ascending time order, starting at ``since``."""

    async def search(self, query: str) -> list[Instrument]: ...

    async def close(self) -> None: ...
