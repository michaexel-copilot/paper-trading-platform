import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

from app.marketdata.base import Bar, Instrument, NotListed, Quote, SourceError, to_decimal

# yfinance is blocking; two worker threads are the concurrency limit towards Yahoo.
MAX_CONCURRENCY = 2

TYPE_BY_QUOTE_TYPE = {
    "EQUITY": "equity",
    "ETF": "etf",
    "CRYPTOCURRENCY": "crypto",
    "CURRENCY": "currency",
    "FUTURE": "future",
}

# Yahoo's bid and ask are often stale or crossed, above all for currencies and
# outside the regular session. They are used only when they are uncrossed, tight
# and sit next to the last price; otherwise the quote is treated as last-price-only.
MAX_PLAUSIBLE_SPREAD = Decimal("0.02")
LAST_PRICE_TOLERANCE = Decimal("0.001")


def _plausible_bid_ask(last: Decimal, bid: Decimal | None, ask: Decimal | None) -> bool:
    if not bid or not ask or bid <= 0 or ask < bid:
        return False
    if (ask - bid) / last > MAX_PLAUSIBLE_SPREAD:
        return False
    slack = last * LAST_PRICE_TOLERANCE
    return bid - slack <= last <= ask + slack


def quote_from_info(symbol: str, info: dict) -> Quote:
    last = to_decimal(info.get("regularMarketPrice"))
    if last is None or last <= 0:
        raise NotListed(f"yahoo has no price for {symbol}")
    bid, ask = to_decimal(info.get("bid")), to_decimal(info.get("ask"))
    if not _plausible_bid_ask(last, bid, ask):
        bid = ask = None
    observed = info.get("regularMarketTime")
    observed_at = datetime.fromtimestamp(observed, UTC) if observed else datetime.now(UTC)
    return Quote(
        last=last,
        bid=bid,
        ask=ask,
        currency=info.get("currency") or "USD",
        observed_at=observed_at,
        source="yahoo",
    )


def instrument_from_info(symbol: str, info: dict) -> Instrument:
    quote_type = info.get("quoteType")
    if not quote_type or info.get("regularMarketPrice") is None:
        raise NotListed(f"yahoo does not list {symbol}")
    return Instrument(
        symbol=symbol,
        name=info.get("longName") or info.get("shortName") or symbol,
        instrument_type=TYPE_BY_QUOTE_TYPE.get(quote_type, quote_type.lower()),
        quote_currency=info.get("currency") or "USD",
        exchange=info.get("exchange"),
    )


def instrument_from_search(row: dict) -> Instrument:
    quote_type = row.get("quoteType") or ""
    return Instrument(
        symbol=row["symbol"],
        name=row.get("longname") or row.get("shortname") or row["symbol"],
        instrument_type=TYPE_BY_QUOTE_TYPE.get(quote_type, quote_type.lower() or "other"),
        quote_currency="",
        exchange=row.get("exchange"),
    )


def bars_from_frame(frame, timeframe: str) -> list[Bar]:
    bars = []
    for index, row in frame.iterrows():
        close = to_decimal(row["Close"])
        if close is None:
            continue
        if timeframe == "1d":
            # Daily bars are stamped at local midnight; keep the calendar date.
            when = datetime(index.year, index.month, index.day, tzinfo=UTC)
        else:
            when = index.to_pydatetime().astimezone(UTC)
        bars.append(
            Bar(
                time=when,
                open=to_decimal(row["Open"]) or close,
                high=to_decimal(row["High"]) or close,
                low=to_decimal(row["Low"]) or close,
                close=close,
                volume=to_decimal(row.get("Volume")),
            )
        )
    return bars


class YahooSource:
    """Yahoo Finance through yfinance. Unofficial endpoints, no API key."""

    name = "yahoo"

    def __init__(self, yf=None):
        if yf is None:
            import yfinance as yf
        self._yf = yf
        self._pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY, thread_name_prefix="yahoo")

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, fn, *args)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"yahoo: {type(exc).__name__}: {exc}") from exc

    def _info(self, symbol: str) -> dict:
        return self._yf.Ticker(symbol).info or {}

    async def load_markets(self) -> dict[str, Instrument]:
        # Yahoo cannot enumerate its instruments; use instrument() or search().
        return {}

    async def instrument(self, symbol: str) -> Instrument:
        return instrument_from_info(symbol, await self._run(self._info, symbol))

    async def fetch_ticker(self, symbol: str) -> Quote:
        return quote_from_info(symbol, await self._run(self._info, symbol))

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: datetime, limit: int | None = None
    ) -> list[Bar]:
        def history():
            return self._yf.Ticker(symbol).history(
                start=since, interval=timeframe, auto_adjust=False, actions=False
            )

        bars = bars_from_frame(await self._run(history), timeframe)
        return bars[:limit] if limit else bars

    async def search(self, query: str) -> list[Instrument]:
        if not query.strip():
            return []

        def search():
            return self._yf.Search(query, max_results=10, news_count=0).quotes

        rows = await self._run(search)
        return [instrument_from_search(row) for row in rows if row.get("symbol")]

    async def trading_hours(self, symbol: str) -> tuple[str, str, str] | None:
        """Timezone and regular session times, for exchanges without a known calendar."""

        def metadata():
            return self._yf.Ticker(symbol).get_history_metadata()

        meta = await self._run(metadata)
        regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        timezone = meta.get("exchangeTimezoneName")
        if not timezone or "start" not in regular or "end" not in regular:
            return None
        return timezone, regular["start"].strftime("%H:%M"), regular["end"].strftime("%H:%M")

    async def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
