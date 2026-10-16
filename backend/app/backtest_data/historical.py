"""Historical OHLCV for backtests, sourced from OKX's public REST endpoints only.

HED-119: there is no live/paper execution path here and no exchange key is read
or needed -- this module only calls public, unauthenticated ccxt endpoints
(``load_markets``, ``fetch_ohlcv``) and writes to its own cache directory,
separate from the trading database. It must stay that way: a backtest data
path has no business importing anything from ``app.trading`` or ``app.models``.

Why this does not reuse ``app.marketdata.ccxt_adapter.CcxtSource.fetch_ohlcv``
for the initial fetch: that method pages forward from ``since`` but gives up
the moment one page comes back empty (see its ``for _ in range(...): ... if
not new: break``). That is fine for the live/paper app, which only ever asks
for a few years of recent history for an asset that is already trading. It is
wrong for a backtest, which must walk forward from a date before *any* OKX
listing to find out where an instrument's history actually starts. Starting
too early must not be mistaken for "no history exists" -- it must skip
forward past the empty window and keep looking. ``_collect`` below does that.
This is a reusable building block, not a one-off hack: the discovery loop and
the gap scan share one pass over the same merged series.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import ccxt.async_support as ccxt_async
import pandas as pd

from app.marketdata.base import Bar, NotListed, to_decimal

log = logging.getLogger(__name__)

TIMEFRAMES = ("1d", "1h")
TIMEFRAME_STEP = {"1d": timedelta(days=1), "1h": timedelta(hours=1)}

# Well before OKX (formerly OKEx) existed. The discovery loop in _collect
# walks forward from here, skipping empty windows, until it finds the
# instrument's actual first candle -- or gives up at MAX_EMPTY_WINDOWS.
EARLIEST_SINCE = datetime(2015, 1, 1, tzinfo=UTC)

PAGE_LIMIT = 300  # OKX's max candles per request (ccxt okx.py: maxLimit = 300)
MAX_ITERATIONS = 4000  # hard stop regardless of progress -- see module docstring
MAX_EMPTY_WINDOWS = 4000  # ~ "walk forward 4000 pages before giving up on a symbol"

# HED-10 Beschluss 1 / HED-130: Hedgeclip's own OKX rate limit is 300 calls per
# minute, stricter than ccxt's okx default of ~545/min (110 ms). enableRateLimit
# alone only enforces ccxt's default, so the exchange's own rateLimit (ms
# between calls) must be set explicitly, or a cold-start discovery run across
# several symbols/1h bars can exceed the catalog's own limit.
OKX_RATE_LIMIT_MS = 200  # 60_000 / 200 = 300 calls/minute

# Default symbols for *technical* validation of this pipeline only -- not the
# Family A strategy universe (that is the Quant Researcher's decision; see
# design note HED-119 for the measured history-depth finding that bears on
# it). USDC first, matching the EWR-account convention from HED-99.
DEFAULT_UNIVERSE = ("BTC/USDC", "ETH/USDC", "SOL/USDC")


@dataclass(frozen=True, slots=True)
class Gap:
    """A run of missing bars between two bars that are both actually on record.

    Never includes the time before the first bar -- that is ``listing_start``,
    not a gap: the instrument simply did not trade yet.
    """

    after: datetime
    before: datetime
    missing_bars: int


@dataclass(frozen=True, slots=True)
class HistoryResult:
    symbol: str
    timeframe: str
    exchange: str
    frame: pd.DataFrame  # columns Open/High/Low/Close/Volume, DatetimeIndex "Date" (UTC)
    gaps: list[Gap]
    listing_start: datetime | None  # first bar OKX has -- None if the symbol has no data at all
    bar_count: int
    fetched_at: datetime


class HistoryCache:
    """Disk cache of raw OHLCV bars, one CSV file per exchange/timeframe/symbol.

    A refresh only ever asks the source for bars after the last cached
    timestamp (see ``refresh``); this class itself just stores whatever it is
    given. Written atomically (write to a temp file, then rename) so a reader
    never sees a half-written file.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, exchange: str, symbol: str, timeframe: str) -> Path:
        safe_symbol = symbol.replace("/", "-")
        return self.root / exchange / timeframe / f"{safe_symbol}.csv"

    def read(self, exchange: str, symbol: str, timeframe: str) -> list[Bar]:
        path = self._path(exchange, symbol, timeframe)
        if not path.exists():
            return []
        bars = []
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                bars.append(
                    Bar(
                        time=datetime.fromisoformat(row["timestamp"]),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]) if row["volume"] else None,
                    )
                )
        return bars

    def write(self, exchange: str, symbol: str, timeframe: str, bars: list[Bar]) -> None:
        path = self._path(exchange, symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(("timestamp", "open", "high", "low", "close", "volume"))
            for bar in bars:
                writer.writerow(
                    (
                        bar.time.astimezone(UTC).isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume if bar.volume is not None else "",
                    )
                )
        tmp.replace(path)


def _merge(old: list[Bar], new: list[Bar]) -> list[Bar]:
    """Combine two bar lists, later values winning on a timestamp collision."""
    by_time = {bar.time: bar for bar in old}
    by_time.update({bar.time: bar for bar in new})
    return [by_time[t] for t in sorted(by_time)]


def find_gaps(bars: list[Bar], timeframe: str) -> list[Gap]:
    """Runs of missing bars strictly between two bars that are on record.

    Crypto trades every day of the year, so for a clean series the gap
    between consecutive bars is always exactly one step; a bigger gap means
    OKX (or this fetch) actually missed something and that must surface, not
    be interpolated away.
    """
    step = TIMEFRAME_STEP[timeframe]
    tolerance = step * 1.5
    gaps = []
    for prev, curr in zip(bars, bars[1:], strict=False):
        delta = curr.time - prev.time
        if delta > tolerance:
            missing = round(delta / step) - 1
            gaps.append(Gap(after=prev.time, before=curr.time, missing_bars=missing))
    return gaps


def to_frame(bars: list[Bar]) -> pd.DataFrame:
    """Bars as a DataFrame: columns Open/High/Low/Close/Volume, index "Date" (UTC)."""
    index = pd.DatetimeIndex([bar.time for bar in bars], tz="UTC", name="Date")
    return pd.DataFrame(
        {
            "Open": [float(bar.open) for bar in bars],
            "High": [float(bar.high) for bar in bars],
            "Low": [float(bar.low) for bar in bars],
            "Close": [float(bar.close) for bar in bars],
            "Volume": [float(bar.volume) if bar.volume is not None else 0.0 for bar in bars],
        },
        index=index,
    )


def _rows_to_bars(rows: dict[int, list]) -> list[Bar]:
    return [
        Bar(
            time=datetime.fromtimestamp(ts / 1000, UTC),
            open=to_decimal(row[1]),
            high=to_decimal(row[2]),
            low=to_decimal(row[3]),
            close=to_decimal(row[4]),
            volume=to_decimal(row[5]),
        )
        for ts, row in sorted(rows.items())
        if row[4] is not None
    ]


async def _collect(
    exchange,
    symbol: str,
    timeframe: str,
    since: datetime,
    now: datetime,
    page_limit: int = PAGE_LIMIT,
    max_iterations: int = MAX_ITERATIONS,
    max_empty_windows: int = MAX_EMPTY_WINDOWS,
) -> list[Bar]:
    step = TIMEFRAME_STEP[timeframe]
    cursor = since
    rows: dict[int, list] = {}
    empty_windows = 0
    for _ in range(max_iterations):
        if cursor >= now:
            break
        page = await exchange.fetch_ohlcv(
            symbol, timeframe, int(cursor.timestamp() * 1000), page_limit
        )
        if not page:
            # No data in this window. Unlike the live/paper adapter, do not
            # give up here -- this is exactly what happens when `since` is
            # before the instrument was first listed. Skip past the window
            # and keep looking.
            empty_windows += 1
            if empty_windows > max_empty_windows:
                break
            cursor = cursor + step * page_limit
            continue
        new = False
        for row in page:
            if row[0] not in rows:
                rows[row[0]] = row
                new = True
        if not new:
            break
        empty_windows = 0
        cursor = datetime.fromtimestamp(max(rows) / 1000, UTC) + step
    return _rows_to_bars(rows)


async def _validate_symbol(exchange, symbol: str) -> None:
    markets = await exchange.load_markets()
    market = markets.get(symbol)
    if market is None or not market.get("spot"):
        raise NotListed(f"okx does not list {symbol} as a spot market")


async def refresh(
    cache: HistoryCache,
    symbol: str,
    timeframe: str,
    exchange_id: str = "okx",
    exchange=None,
    now: datetime | None = None,
) -> HistoryResult:
    """Fetch bars newer than the cache, merge, persist, and report gaps.

    Only ever asks the source for bars after the cache's last timestamp (or,
    for a cold cache, walks forward from ``EARLIEST_SINCE`` to find the
    instrument's real start). Pass ``exchange`` in tests to replay a fixture
    instead of hitting the network.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {timeframe!r}, expected one of {TIMEFRAMES}")
    now = now or datetime.now(UTC)
    owns_exchange = exchange is None
    exchange = exchange or getattr(ccxt_async, exchange_id)(
        {"enableRateLimit": True, "rateLimit": OKX_RATE_LIMIT_MS}
    )
    try:
        await _validate_symbol(exchange, symbol)
        cached = cache.read(exchange_id, symbol, timeframe)
        since = cached[-1].time + TIMEFRAME_STEP[timeframe] if cached else EARLIEST_SINCE
        fresh = await _collect(exchange, symbol, timeframe, since, now)
        merged = _merge(cached, fresh)
    finally:
        if owns_exchange:
            await exchange.close()
    cache.write(exchange_id, symbol, timeframe, merged)
    return HistoryResult(
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange_id,
        frame=to_frame(merged),
        gaps=find_gaps(merged, timeframe),
        listing_start=merged[0].time if merged else None,
        bar_count=len(merged),
        fetched_at=now,
    )
