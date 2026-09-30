import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.dbtypes import utcnow
from app.marketdata.base import Bar, DataUnavailable, Quote
from app.marketdata.cache import TTLCache, quote_ttl
from app.marketdata.calendars import MarketStatus, market_status
from app.marketdata.router import AssetRef, SourceRouter

log = logging.getLogger(__name__)

# A quote older than this is stale.
FRESH_SECONDS = {"crypto": 30.0}
DEFAULT_FRESH_SECONDS = 20 * 60.0
# A fresh quote older than this is shown as delayed.
DELAYED_AFTER_SECONDS = 120.0

HISTORY_TTL_SECONDS = 600.0
DEFAULT_HISTORY_SPAN = {"1d": timedelta(days=5 * 366), "1h": timedelta(days=30)}

# Stablecoins and sub-units, expressed as (currency, multiplier to that currency).
CURRENCY_ALIASES = {
    "USDT": ("USD", Decimal(1)),
    "USDC": ("USD", Decimal(1)),
    "GBp": ("GBP", Decimal("0.01")),
    "GBX": ("GBP", Decimal("0.01")),
    "USX": ("USD", Decimal("0.01")),  # US cents, used for grain futures
}


def fresh_limit(asset_class: str) -> float:
    return FRESH_SECONDS.get(asset_class, DEFAULT_FRESH_SECONDS)


def classify(
    asset_class: str, observed_at: datetime, now: datetime, market_open: bool
) -> tuple[bool, bool, float]:
    """Return (fresh, delayed, age in seconds) for a quote."""
    age = max(0.0, (now - observed_at).total_seconds())
    fresh = market_open and age <= fresh_limit(asset_class)
    delayed = fresh and asset_class != "crypto" and age > DELAYED_AFTER_SECONDS
    return fresh, delayed, age


@dataclass(frozen=True, slots=True)
class QuoteView:
    quote: Quote
    market_open: bool
    next_open: datetime | None
    age_seconds: float
    fresh: bool
    delayed: bool

    @property
    def stale(self) -> bool:
        return not self.fresh


@dataclass(frozen=True, slots=True)
class FxRate:
    rate: Decimal
    observed_at: datetime | None
    source: str | None


def normalize_currency(currency: str) -> tuple[str, Decimal]:
    return CURRENCY_ALIASES.get(currency, (currency.upper(), Decimal(1)))


class MarketService:
    """Quotes, market status, history and conversion rates for catalog assets."""

    def __init__(
        self,
        router: SourceRouter,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.router = router
        self.now = now
        self._quotes = TTLCache(monotonic)
        self._history = TTLCache(monotonic)
        # Called with (asset id, quote) after each fetch from a source.
        self.recorder: Callable[[int, Quote], Awaitable[None]] | None = None

    @staticmethod
    def ref(asset) -> AssetRef:
        return AssetRef(
            key=asset.id,
            asset_class=asset.asset_class,
            symbols={link.source: link.symbol for link in asset.source_symbols},
        )

    def status(self, asset) -> MarketStatus:
        return market_status(asset.calendar, self.now())

    async def quote(self, asset) -> Quote:
        ref = self.ref(asset)

        async def fetch() -> Quote:
            quote = await self.router.fetch_quote(ref)
            if self.recorder is not None:
                try:
                    await self.recorder(asset.id, quote)
                except Exception:
                    log.exception("recording the price of asset %s failed", asset.id)
            return quote

        return await self._quotes.get(("asset", asset.id), quote_ttl(asset.asset_class), fetch)

    def peek(self, asset) -> Quote | None:
        """The last quote seen for the asset, without contacting a source."""
        return self._quotes.peek(("asset", asset.id))

    def _view(self, asset, quote: Quote) -> QuoteView:
        status = self.status(asset)
        fresh, delayed, age = classify(
            asset.asset_class, quote.observed_at, self.now(), status.is_open
        )
        return QuoteView(quote, status.is_open, status.next_open, age, fresh, delayed)

    async def view(self, asset) -> QuoteView:
        return self._view(asset, await self.quote(asset))

    def peek_view(self, asset) -> QuoteView | None:
        quote = self.peek(asset)
        return self._view(asset, quote) if quote else None

    async def fx(self, from_currency: str, to_currency: str) -> FxRate:
        """Conversion rate: one unit of ``from_currency`` in ``to_currency``."""
        source_ccy, source_factor = normalize_currency(from_currency)
        target_ccy, target_factor = normalize_currency(to_currency)
        factor = source_factor / target_factor
        if source_ccy == target_ccy:
            return FxRate(factor, None, None)

        # EUR/USD is always taken from the EURUSD quote, in either direction.
        invert = (source_ccy, target_ccy) == ("USD", "EUR")
        pair = "EURUSD" if invert else f"{source_ccy}{target_ccy}"
        ref = AssetRef(
            key=("fx", pair),
            asset_class="forex",
            symbols={name: f"{pair}=X" for name in self.router.chains.get("forex", [])},
        )
        quote: Quote = await self._quotes.get(
            ref.key, quote_ttl("forex"), lambda: self.router.fetch_quote(ref)
        )
        if quote.last <= 0:
            raise DataUnavailable(f"no usable rate for {pair}")
        rate = Decimal(1) / quote.last if invert else quote.last
        return FxRate(rate * factor, quote.observed_at, quote.source)

    async def history(
        self, asset, timeframe: str, start: datetime | None = None, end: datetime | None = None
    ) -> tuple[str | None, list[Bar]]:
        now = self.now()
        end = end or now
        start = start or end - DEFAULT_HISTORY_SPAN[timeframe]
        if start >= end:
            return None, []
        ref = self.ref(asset)
        key = ("history", asset.id, timeframe, start.date().isoformat())
        source, bars = await self._history.get(
            key,
            HISTORY_TTL_SECONDS,
            lambda: self.router.fetch_history(ref, timeframe, start),
        )
        return source, [bar for bar in bars if start <= bar.time <= end]

    async def close(self) -> None:
        await self.router.close()
