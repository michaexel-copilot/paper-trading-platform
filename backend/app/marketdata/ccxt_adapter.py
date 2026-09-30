import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import ccxt
import ccxt.async_support as ccxt_async

from app.marketdata.base import Bar, Instrument, NotListed, Quote, SourceError, to_decimal

MARKETS_TTL_SECONDS = 24 * 3600
# Quote currencies offered by search, in order of preference.
SEARCH_QUOTES = ("USDT", "USD", "USDC", "EUR")
OHLCV_PAGE_LIMIT = 40


def _amount_step(exchange, market: dict) -> Decimal | None:
    precision = (market.get("precision") or {}).get("amount")
    if precision is None:
        return None
    if getattr(exchange, "precisionMode", ccxt.TICK_SIZE) == ccxt.DECIMAL_PLACES:
        return Decimal(1).scaleb(-int(precision))
    return to_decimal(precision)


def _to_instrument(exchange, market: dict) -> Instrument:
    limits = (market.get("limits") or {}).get("amount") or {}
    return Instrument(
        symbol=market["symbol"],
        name=market.get("base") or market["symbol"],
        instrument_type="crypto",
        quote_currency=market["quote"],
        amount_step=_amount_step(exchange, market),
        min_amount=to_decimal(limits.get("min")),
        maker=to_decimal(market.get("maker")),
        taker=to_decimal(market.get("taker")),
        exchange=exchange.id,
        base=market.get("base"),
    )


class CcxtSource:
    """Any ccxt spot exchange as a price source, using public endpoints only."""

    def __init__(self, exchange_id: str, exchange=None):
        self.name = exchange_id
        self._exchange = exchange
        self._markets: dict[str, Instrument] | None = None
        self._markets_loaded_at = 0.0
        self._lock = asyncio.Lock()

    def _ex(self):
        if self._exchange is None:
            self._exchange = getattr(ccxt_async, self.name)({"enableRateLimit": True})
        return self._exchange

    async def _call(self, method: str, *args, **kwargs):
        try:
            return await getattr(self._ex(), method)(*args, **kwargs)
        except ccxt.BadSymbol as exc:
            raise NotListed(f"{self.name} does not list {args[0] if args else '?'}") from exc
        except ccxt.BaseError as exc:
            raise SourceError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    async def load_markets(self) -> dict[str, Instrument]:
        async with self._lock:
            fresh = time.monotonic() - self._markets_loaded_at < MARKETS_TTL_SECONDS
            if self._markets is None or not fresh:
                raw = await self._call("load_markets", self._markets is not None)
                exchange = self._ex()
                self._markets = {
                    symbol: _to_instrument(exchange, market)
                    for symbol, market in raw.items()
                    if market.get("spot") and market.get("active") is not False
                }
                self._markets_loaded_at = time.monotonic()
            return self._markets

    async def instrument(self, symbol: str) -> Instrument:
        markets = await self.load_markets()
        if symbol not in markets:
            raise NotListed(f"{self.name} does not list {symbol}")
        return markets[symbol]

    async def fetch_ticker(self, symbol: str) -> Quote:
        instrument = await self.instrument(symbol)
        ticker = await self._call("fetch_ticker", symbol)
        last = to_decimal(ticker.get("last")) or to_decimal(ticker.get("close"))
        if last is None:
            raise SourceError(f"{self.name} returned no price for {symbol}")
        timestamp = ticker.get("timestamp")
        observed_at = (
            datetime.fromtimestamp(timestamp / 1000, UTC) if timestamp else datetime.now(UTC)
        )
        bid, ask = to_decimal(ticker.get("bid")), to_decimal(ticker.get("ask"))
        if not (bid and ask and 0 < bid <= ask):
            bid = ask = None
        return Quote(
            last=last,
            bid=bid,
            ask=ask,
            currency=instrument.quote_currency,
            observed_at=observed_at,
            source=self.name,
        )

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: datetime, limit: int | None = None
    ) -> list[Bar]:
        await self.instrument(symbol)
        cursor = int(since.timestamp() * 1000)
        rows: dict[int, list] = {}
        # Exchanges cap the bars per request, so page forward until caught up.
        for _ in range(OHLCV_PAGE_LIMIT):
            page = await self._call("fetch_ohlcv", symbol, timeframe, cursor, None)
            new = [row for row in page if row[0] not in rows and row[0] >= cursor]
            if not new:
                break
            for row in new:
                rows[row[0]] = row
            cursor = max(rows) + 1
            if limit and len(rows) >= limit:
                break
        bars = [
            Bar(
                time=datetime.fromtimestamp(row[0] / 1000, UTC),
                open=to_decimal(row[1]),
                high=to_decimal(row[2]),
                low=to_decimal(row[3]),
                close=to_decimal(row[4]),
                volume=to_decimal(row[5]),
            )
            for _, row in sorted(rows.items())
            if row[4] is not None
        ]
        return bars[:limit] if limit else bars

    async def search(self, query: str) -> list[Instrument]:
        needle = query.strip().upper()
        if not needle:
            return []
        markets = await self.load_markets()
        by_base: dict[str, Instrument] = {}
        for instrument in markets.values():
            if instrument.quote_currency not in SEARCH_QUOTES or not instrument.base:
                continue
            if needle not in instrument.base.upper():
                continue
            current = by_base.get(instrument.base)
            if current is None or SEARCH_QUOTES.index(instrument.quote_currency) < (
                SEARCH_QUOTES.index(current.quote_currency)
            ):
                by_base[instrument.base] = instrument
        # Exact matches first, then alphabetically.
        ordered = sorted(by_base.values(), key=lambda i: (i.base.upper() != needle, i.base))
        return ordered[:10]

    async def close(self) -> None:
        if self._exchange is not None:
            await self._exchange.close()
