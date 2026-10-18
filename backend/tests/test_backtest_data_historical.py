"""HED-119: historical OKX OHLCV for backtests -- fetch, cache, gap detection.

Offline tests replay a fake exchange (same idea as RecordedExchange in
test_marketdata.py); the one @pytest.mark.live test hits the real public OKX
endpoint and is excluded from the default run (see pyproject.toml addopts).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import app.backtest_data.historical as historical
from app.backtest_data.historical import (
    EARLIEST_SINCE,
    MAX_INTRADAY_RANGE_RATIO,
    OKX_RATE_LIMIT_MS,
    Gap,
    HistoryCache,
    find_gaps,
    implausibility_reason,
    refresh,
    to_frame,
)
from app.marketdata.base import Bar, NotListed, to_decimal


def bar(day_offset: int, start: datetime, price: float = 100.0) -> Bar:
    t = start + timedelta(days=day_offset)
    return Bar(
        time=t,
        open=Decimal(str(price)),
        high=Decimal(str(price + 1)),
        low=Decimal(str(price - 1)),
        close=Decimal(str(price)),
        volume=Decimal("10"),
    )


def rows_from(bars: list[Bar]) -> list[list]:
    return [
        [
            int(b.time.timestamp() * 1000),
            float(b.open),
            float(b.high),
            float(b.low),
            float(b.close),
            float(b.volume),
        ]
        for b in bars
    ]


class FakeExchange:
    """Replaces ccxt's async okx exchange: load_markets + fetch_ohlcv only.

    Mimics OKX's real windowing (ccxt okx.py sets ``after = since +
    duration*limit``): a call only ever sees rows inside
    ``[since, since + limit * step)``, so a ``since`` before any real data
    genuinely comes back empty -- exactly the case _collect must skip past.
    """

    _STEP_MS = {"1d": 86_400_000, "1h": 3_600_000}

    def __init__(self, markets: dict, rows: list[list]):
        self.markets = markets
        self.rows = sorted(rows, key=lambda r: r[0])
        self.calls: list[tuple] = []
        self.closed = False

    async def load_markets(self):
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls.append((symbol, timeframe, since, limit))
        until = since + self._STEP_MS[timeframe] * limit
        page = [r for r in self.rows if since <= r[0] < until]
        return page[:limit]

    async def close(self):
        self.closed = True


SPOT_MARKETS = {
    "BTC/USDC": {"spot": True},
    "ETH/USDC": {"spot": True},
    "SWAP/USDC": {"spot": False},
}


# --- gap detection -----------------------------------------------------------------


def test_find_gaps_is_empty_for_a_clean_daily_series():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars = [bar(i, start) for i in range(10)]

    assert find_gaps(bars, "1d") == []


def test_find_gaps_reports_missing_trading_days_not_the_listing_start():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    # day 0,1,2, then a jump to day 7 (4 missing days), then 7,8.
    bars = [bar(0, start), bar(1, start), bar(2, start), bar(7, start), bar(8, start)]

    gaps = find_gaps(bars, "1d")

    assert gaps == [Gap(after=bars[2].time, before=bars[3].time, missing_bars=4)]


def test_find_gaps_tolerates_exact_step_jitter_but_not_more():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    on_time = bar(0, start)
    one_step_late = Bar(
        time=start + timedelta(days=1, minutes=1),
        open=on_time.open,
        high=on_time.high,
        low=on_time.low,
        close=on_time.close,
        volume=on_time.volume,
    )

    assert find_gaps([on_time, one_step_late], "1d") == []


# --- disk cache ---------------------------------------------------------------------


def test_cache_roundtrips_bars_including_decimal_precision(tmp_path):
    cache = HistoryCache(tmp_path)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    original = [
        Bar(
            time=start,
            open=Decimal("0.1"),
            high=Decimal("0.123456789"),
            low=Decimal("0.099999999"),
            close=Decimal("0.11"),
            volume=None,
        )
    ]

    cache.write("okx", "BTC/USDC", "1d", original)
    restored = cache.read("okx", "BTC/USDC", "1d")

    assert restored == original


def test_cache_read_of_missing_file_is_an_empty_list(tmp_path):
    cache = HistoryCache(tmp_path)

    assert cache.read("okx", "NOPE/USDC", "1d") == []


def test_cache_write_is_atomic_no_stray_tmp_file(tmp_path):
    cache = HistoryCache(tmp_path)
    cache.write("okx", "BTC/USDC", "1d", [bar(0, datetime(2024, 1, 1, tzinfo=UTC))])

    path = cache._path("okx", "BTC/USDC", "1d")
    assert path.exists()
    assert not path.with_suffix(".csv.tmp").exists()


# --- to_frame -------------------------------------------------------------------------


def test_to_frame_has_the_shape_a_backtest_engine_expects():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars = [bar(0, start, 100.0), bar(1, start, 101.0)]

    frame = to_frame(bars)

    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.index.name == "Date"
    assert str(frame.index.tz) == "UTC"
    assert frame.iloc[0]["Close"] == 100.0
    assert frame.iloc[1]["Close"] == 101.0


# --- HED-155: price plausibility filter ----------------------------------------------
#
# Fixture values are the real OKX artifact found on HED-131 (bisection on BTC/USDT and
# ETH/USDT, 2017-10-10): a single placeholder candle with open=1, low=1 USD, while
# close already sits at the real traded level.

_2017_10_10_MS = int(datetime(2017, 10, 10, tzinfo=UTC).timestamp() * 1000)
BTC_USDT_ARTIFACT_2017_10_10 = (_2017_10_10_MS, 1.0, 1.0, 1.0, 4901.0, 19.26)
ETH_USDT_ARTIFACT_2017_10_10 = (_2017_10_10_MS, 1.0, 1.0, 1.0, 300.0, 260.8)


def test_implausibility_reason_flags_the_known_btc_usdt_artifact():
    reason = implausibility_reason(*(to_decimal(v) for v in (1, 1, 1, 4901)))

    assert reason is not None
    assert "4901" in reason


def test_implausibility_reason_flags_the_known_eth_usdt_artifact():
    reason = implausibility_reason(*(to_decimal(v) for v in (1, 1, 1, 300)))

    assert reason is not None


def test_implausibility_reason_accepts_an_ordinary_trading_day():
    reason = implausibility_reason(*(to_decimal(v) for v in (100, 101, 99, 100)))

    assert reason is None


def test_implausibility_reason_accepts_a_violent_but_real_looking_day_at_the_threshold():
    # Just inside MAX_INTRADAY_RANGE_RATIO (20x): a bad day, not an artifact.
    low = Decimal("10")
    high = low * MAX_INTRADAY_RANGE_RATIO - Decimal("0.01")

    reason = implausibility_reason(low, high, low, high)

    assert reason is None


def test_implausibility_reason_rejects_a_non_positive_price():
    reason = implausibility_reason(*(to_decimal(v) for v in (0, 1, 0, 1)))

    assert reason is not None


def test_rows_to_bars_drops_the_artifact_candle_but_keeps_it_visible():
    real_next_day_ms = int(datetime(2017, 10, 11, tzinfo=UTC).timestamp() * 1000)
    real_next_day = (real_next_day_ms, 4901.0, 5100.0, 4850.0, 4950.0, 1000.0)
    rows = {
        BTC_USDT_ARTIFACT_2017_10_10[0]: list(BTC_USDT_ARTIFACT_2017_10_10),
        real_next_day[0]: list(real_next_day),
    }

    bars, implausible = historical._rows_to_bars(rows)

    assert [b.close for b in bars] == [Decimal("4950.0")]
    assert len(implausible) == 1
    dropped = implausible[0]
    assert dropped.open == Decimal("1.0")
    assert dropped.close == Decimal("4901.0")
    assert "Intraday-Preisspanne" in dropped.reason


async def test_refresh_drops_the_artifact_candle_and_reports_it_in_the_result(tmp_path):
    listing_start = datetime(2017, 10, 11, tzinfo=UTC)
    real_bars = [bar(i, listing_start, price=4901.0 + i * 10) for i in range(3)]
    artifact_row = list(BTC_USDT_ARTIFACT_2017_10_10)
    artifact_row[0] = int((listing_start - timedelta(days=1)).timestamp() * 1000)
    rows = [artifact_row, *rows_from(real_bars)]
    exchange = FakeExchange(SPOT_MARKETS, rows)
    cache = HistoryCache(tmp_path)

    result = await refresh(cache, "BTC/USDC", "1d", exchange=exchange)

    assert result.bar_count == 3
    assert result.listing_start == listing_start  # not the artifact day before it
    assert len(result.implausible_bars) == 1
    assert result.implausible_bars[0].time == listing_start - timedelta(days=1)


# --- refresh(): discovery, caching, gaps, all together -------------------------------


async def test_refresh_rejects_a_symbol_okx_does_not_list(tmp_path):
    exchange = FakeExchange(SPOT_MARKETS, rows=[])
    cache = HistoryCache(tmp_path)  # never touched: validation fails first

    with pytest.raises(NotListed):
        await refresh(cache, "NOPE/USDC", "1d", exchange=exchange)


async def test_refresh_skips_forward_past_empty_windows_to_find_the_real_start(tmp_path):
    listing_start = EARLIEST_SINCE + timedelta(days=1190)  # several empty 300-day windows away
    real_bars = [bar(i, listing_start) for i in range(5)]
    exchange = FakeExchange(SPOT_MARKETS, rows_from(real_bars))
    cache = HistoryCache(tmp_path)

    result = await refresh(cache, "BTC/USDC", "1d", exchange=exchange)

    assert result.bar_count == 5
    assert result.listing_start == listing_start
    assert result.gaps == []
    assert exchange.calls[0][2] == int(EARLIEST_SINCE.timestamp() * 1000)
    assert exchange.closed is False  # caller passed the exchange in, refresh must not close it


async def test_refresh_only_asks_for_bars_after_the_cache_on_a_warm_run(tmp_path):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    cache = HistoryCache(tmp_path)
    first_batch = [bar(i, start) for i in range(3)]
    exchange = FakeExchange(SPOT_MARKETS, rows_from(first_batch))
    await refresh(cache, "BTC/USDC", "1d", exchange=exchange)

    second_batch = first_batch + [bar(i, start) for i in range(3, 6)]
    exchange2 = FakeExchange(SPOT_MARKETS, rows_from(second_batch))
    result = await refresh(cache, "BTC/USDC", "1d", exchange=exchange2)

    assert result.bar_count == 6
    expected_since = int((first_batch[-1].time + timedelta(days=1)).timestamp() * 1000)
    assert exchange2.calls[0][2] == expected_since


async def test_refresh_reports_a_gap_that_appears_after_cached_history(tmp_path):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    cache = HistoryCache(tmp_path)
    exchange = FakeExchange(SPOT_MARKETS, rows_from([bar(0, start), bar(1, start)]))
    await refresh(cache, "BTC/USDC", "1d", exchange=exchange)

    later = [bar(0, start), bar(1, start), bar(6, start)]  # a 4-day hole after day 1
    exchange2 = FakeExchange(SPOT_MARKETS, rows_from(later))
    result = await refresh(cache, "BTC/USDC", "1d", exchange=exchange2)

    assert result.bar_count == 3
    assert result.gaps == [Gap(after=later[1].time, before=later[2].time, missing_bars=4)]


# --- HED-130: own OKX rate limit (300/min), not ccxt's default (~545/min) ---------


def test_okx_rate_limit_is_300_per_minute_not_ccxts_default():
    """HED-10 Beschluss 1: Hedgeclip's own OKX limit is 300 calls/minute."""
    assert 60_000 / OKX_RATE_LIMIT_MS == 300


async def test_refresh_configures_ccxt_with_the_own_rate_limit_when_it_builds_the_exchange(
    monkeypatch, tmp_path
):
    """refresh() only enforces the limit when it builds its own exchange (the
    production path, used by refresh_backtest_history.py) -- a caller-supplied
    exchange (as in every other test here) is a fixture, not a rate limit to set.
    """
    captured: list[dict] = []

    class RecordingOkx(FakeExchange):
        def __init__(self, config):
            captured.append(config)
            super().__init__(SPOT_MARKETS, rows=[])

    monkeypatch.setattr(historical.ccxt_async, "okx", RecordingOkx)
    cache = HistoryCache(tmp_path)

    await refresh(cache, "BTC/USDC", "1d")

    assert captured == [{"enableRateLimit": True, "rateLimit": OKX_RATE_LIMIT_MS}]


# --- live: the real OKX public endpoint, no key -----------------------------------


@pytest.mark.live
async def test_live_refresh_btc_usdc(tmp_path):
    cache = HistoryCache(tmp_path)

    result = await refresh(cache, "BTC/USDC", "1d")

    assert result.bar_count > 300
    assert result.listing_start is not None
    assert result.frame.shape[0] == result.bar_count
