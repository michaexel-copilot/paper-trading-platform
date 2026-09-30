import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import ccxt
import pandas as pd
import pytest

from app.marketdata.base import DataUnavailable, MarketDataSource, NotListed, SourceError
from app.marketdata.cache import TTLCache, quote_ttl
from app.marketdata.calendars import market_status
from app.marketdata.ccxt_adapter import CcxtSource
from app.marketdata.router import AssetRef, SourceRouter
from app.marketdata.service import classify
from app.marketdata.yahoo_adapter import YahooSource
from app.models import Asset
from tests.conftest import asset_id
from tests.fakes import START, Clock, FakeSource, daily_bars

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- the source protocol ---------------------------------------------------------


async def test_fake_source_implements_the_whole_protocol():
    clock = Clock()
    source = FakeSource("fake", clock)
    source.add("BTC/USDT", currency="USDT", step="0.0001", minimum="0.001", base="BTC")
    source.price("BTC/USDT", 50000, bid=49990, ask=50010)
    source.bars[("BTC/USDT", "1d")] = daily_bars(START - timedelta(days=3), [1, 2, 3])

    assert isinstance(source, MarketDataSource)
    assert list(await source.load_markets()) == ["BTC/USDT"]
    assert (await source.instrument("BTC/USDT")).amount_step == Decimal("0.0001")
    quote = await source.fetch_ticker("BTC/USDT")
    assert (quote.last, quote.bid, quote.ask) == (50000, 49990, 50010)
    assert quote.observed_at == clock() and not quote.last_price_only
    bars = await source.fetch_ohlcv("BTC/USDT", "1d", START - timedelta(days=2))
    assert [bar.close for bar in bars] == [2, 3]
    assert [i.symbol for i in await source.search("btc")] == ["BTC/USDT"]
    with pytest.raises(NotListed):
        await source.fetch_ticker("NOPE/USDT")
    await source.close()


# --- ccxt adapter -----------------------------------------------------------------


class RecordedExchange:
    """Replays recorded OKX responses in place of a ccxt exchange object."""

    id = "okx"
    precisionMode = ccxt.TICK_SIZE

    def __init__(self):
        self.calls = []

    async def load_markets(self, reload=False):
        self.calls.append("load_markets")
        return fixture("okx_markets.json")

    async def fetch_ticker(self, symbol):
        self.calls.append("fetch_ticker")
        if symbol != "BTC/USDT":
            raise ccxt.BadSymbol(symbol)
        return fixture("okx_ticker_btc_usdt.json")

    async def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls.append("fetch_ohlcv")
        return [row for row in fixture("okx_ohlcv_btc_usdt_1d.json") if row[0] >= since]

    async def close(self):
        self.calls.append("close")


async def test_ccxt_markets_become_instruments_with_exact_decimals():
    source = CcxtSource("okx", RecordedExchange())

    markets = await source.load_markets()

    # The perpetual swap in the recording is not a spot market and is left out.
    assert set(markets) == {"BTC/USDT", "ETH/USDT"}
    btc = markets["BTC/USDT"]
    assert btc.asset_class == "crypto" and btc.quote_currency == "USDT" and btc.base == "BTC"
    assert btc.amount_step == Decimal("1E-8")
    assert btc.min_amount == Decimal("0.00001")
    assert isinstance(btc.maker, Decimal) and isinstance(btc.taker, Decimal)


async def test_ccxt_ticker_becomes_a_quote():
    recorded = fixture("okx_ticker_btc_usdt.json")
    source = CcxtSource("okx", RecordedExchange())

    quote = await source.fetch_ticker("BTC/USDT")

    assert quote.last == Decimal(str(recorded["last"]))
    assert quote.bid == Decimal(str(recorded["bid"])) and quote.ask == Decimal(str(recorded["ask"]))
    assert quote.currency == "USDT" and quote.source == "okx"
    assert quote.observed_at == datetime.fromtimestamp(recorded["timestamp"] / 1000, UTC)


async def test_ccxt_unknown_symbol_is_not_listed_without_a_request():
    exchange = RecordedExchange()
    source = CcxtSource("okx", exchange)

    with pytest.raises(NotListed):
        await source.fetch_ticker("BNB/USDT")
    assert "fetch_ticker" not in exchange.calls


async def test_ccxt_markets_are_loaded_once():
    exchange = RecordedExchange()
    source = CcxtSource("okx", exchange)

    await source.fetch_ticker("BTC/USDT")
    await source.fetch_ticker("BTC/USDT")

    assert exchange.calls.count("load_markets") == 1


async def test_ccxt_errors_become_source_errors():
    class Broken(RecordedExchange):
        async def fetch_ticker(self, symbol):
            raise ccxt.NetworkError("timeout")

    with pytest.raises(SourceError):
        await CcxtSource("okx", Broken()).fetch_ticker("BTC/USDT")


async def test_ccxt_history_is_ascending_and_deduplicated():
    recorded = fixture("okx_ohlcv_btc_usdt_1d.json")
    source = CcxtSource("okx", RecordedExchange())

    bars = await source.fetch_ohlcv("BTC/USDT", "1d", datetime.fromtimestamp(0, UTC))

    assert len(bars) == len(recorded)
    assert [bar.time for bar in bars] == sorted(bar.time for bar in bars)
    assert bars[-1].close == Decimal(str(recorded[-1][4]))


async def test_ccxt_search_matches_the_base_currency():
    source = CcxtSource("okx", RecordedExchange())

    assert [i.symbol for i in await source.search("eth")] == ["ETH/USDT"]
    assert await source.search("zzz") == []


@pytest.mark.live
async def test_live_okx_btc_usdt():
    source = CcxtSource("okx")
    try:
        quote = await source.fetch_ticker("BTC/USDT")
        instrument = await source.instrument("BTC/USDT")
    finally:
        await source.close()
    assert quote.last > 0 and quote.bid <= quote.ask and quote.currency == "USDT"
    assert datetime.now(UTC) - quote.observed_at < timedelta(minutes=5)
    assert instrument.amount_step > 0


# --- Yahoo adapter ------------------------------------------------------------------


class RecordedYahoo:
    """Stands in for the yfinance module, replaying recorded responses."""

    def __init__(self):
        self.info = fixture("yahoo_info.json")
        self.active = 0
        self.peak = 0

    def Ticker(self, symbol):  # noqa: N802 - mirrors yfinance
        module = self

        class Ticker:
            @property
            def info(self):
                module.active += 1
                module.peak = max(module.peak, module.active)
                try:
                    import time

                    time.sleep(0.02)
                    return module.info.get(symbol, {"trailingPegRatio": None})
                finally:
                    module.active -= 1

            def history(self, start, interval, auto_adjust, actions):
                recorded = fixture("yahoo_history_aapl_1d.json")
                frame = pd.DataFrame(
                    recorded["data"],
                    columns=recorded["columns"],
                    index=pd.to_datetime(recorded["index"], utc=True).tz_convert(
                        "America/New_York"
                    ),
                )
                return frame[frame.index >= pd.Timestamp(start)]

        return Ticker()

    def Search(self, query, max_results, news_count):  # noqa: N802 - mirrors yfinance
        class Search:
            quotes = fixture("yahoo_search_apple.json") if "apple" in query.lower() else []

        return Search()


@pytest.fixture
async def yahoo():
    source = YahooSource(RecordedYahoo())
    yield source
    await source.close()


async def test_yahoo_quote_with_tight_bid_and_ask(yahoo):
    quote = await yahoo.fetch_ticker("GC=F")

    assert quote.last == Decimal("4231.9")
    assert (quote.bid, quote.ask) == (Decimal("4232.3"), Decimal("4232.5"))
    assert quote.currency == "USD" and quote.source == "yahoo"
    assert quote.observed_at == datetime.fromtimestamp(1790757703, UTC)


@pytest.mark.parametrize("symbol", ["AAPL", "EURUSD=X", "SPY", "BTC-USD"])
async def test_yahoo_implausible_bid_and_ask_are_dropped(yahoo, symbol):
    """Crossed, zero and missing bid/ask all make the quote last-price-only."""
    quote = await yahoo.fetch_ticker(symbol)

    assert quote.last > 0
    assert quote.last_price_only and quote.bid is None and quote.ask is None


@pytest.mark.parametrize(
    ("symbol", "asset_class", "exchange"),
    [
        ("AAPL", "stocks", "NMS"),
        ("SPY", "etfs", "PCX"),
        ("GC=F", "commodities", "CMX"),
        ("EURUSD=X", "forex", "CCY"),
        ("BTC-USD", "crypto", "CCC"),
        ("VFIAX", None, "NAS"),
        ("AAPL261218C00300000", None, "OPR"),
    ],
)
async def test_yahoo_asset_class_comes_from_the_quote_type(yahoo, symbol, asset_class, exchange):
    instrument = await yahoo.instrument(symbol)

    assert instrument.asset_class == asset_class
    assert instrument.exchange == exchange


async def test_yahoo_unknown_symbol_is_not_listed(yahoo):
    with pytest.raises(NotListed):
        await yahoo.instrument("ZZZZNOPE")
    with pytest.raises(NotListed):
        await yahoo.fetch_ticker("ZZZZNOPE")


async def test_yahoo_daily_history_keeps_the_calendar_date(yahoo):
    bars = await yahoo.fetch_ohlcv("AAPL", "1d", datetime(2026, 1, 1, tzinfo=UTC))

    assert len(bars) == 5
    assert [bar.time for bar in bars] == sorted(bar.time for bar in bars)
    assert bars[-1].time == datetime(2026, 9, 29, tzinfo=UTC)
    assert isinstance(bars[-1].close, Decimal) and bars[-1].close > 0


async def test_yahoo_search_reports_supported_and_unsupported_types(yahoo):
    found = {i.symbol: i.asset_class for i in await yahoo.search("apple")}

    assert found["AAPL"] == "stocks"
    assert await yahoo.search("   ") == []


async def test_yahoo_runs_at_most_two_requests_at_once(yahoo):
    await asyncio.gather(*(yahoo.fetch_ticker("GC=F") for _ in range(12)))

    assert yahoo._yf.peak == 2


async def test_yahoo_failures_become_source_errors():
    class Broken:
        def Ticker(self, symbol):  # noqa: N802
            raise RuntimeError("rate limited")

    source = YahooSource(Broken())
    with pytest.raises(SourceError):
        await source.fetch_ticker("AAPL")
    await source.close()


@pytest.mark.live
@pytest.mark.parametrize(
    ("symbol", "asset_class"),
    [("AAPL", "stocks"), ("SPY", "etfs"), ("GC=F", "commodities"), ("EURUSD=X", "forex")],
)
async def test_live_yahoo(symbol, asset_class):
    source = YahooSource()
    try:
        quote = await source.fetch_ticker(symbol)
        instrument = await source.instrument(symbol)
        bars = await source.fetch_ohlcv(symbol, "1d", datetime.now(UTC) - timedelta(days=30))
    finally:
        await source.close()
    assert quote.last > 0 and quote.currency
    assert instrument.asset_class == asset_class
    assert len(bars) > 10


# --- cache ----------------------------------------------------------------------------


async def test_fifty_concurrent_requests_cause_one_source_call():
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "quote"

    cache = TTLCache()
    results = await asyncio.gather(*(cache.get("AAPL", 60, fetch) for _ in range(50)))

    assert calls == 1 and set(results) == {"quote"}


async def test_cache_refetches_after_the_ttl():
    clock = Clock()
    calls = []

    async def fetch():
        calls.append(clock.elapsed)
        return len(calls)

    cache = TTLCache(clock.monotonic)
    assert await cache.get("k", 60, fetch) == 1
    clock.advance(59)
    assert await cache.get("k", 60, fetch) == 1
    clock.advance(2)
    assert await cache.get("k", 60, fetch) == 2
    assert cache.peek("k") == 2


async def test_a_failed_fetch_is_not_cached():
    attempts = 0

    async def fetch():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SourceError("down")
        return "ok"

    cache = TTLCache()
    with pytest.raises(SourceError):
        await cache.get("k", 60, fetch)
    assert await cache.get("k", 60, fetch) == "ok"


def test_quote_ttl_per_asset_class():
    assert quote_ttl("crypto") == 5
    assert {quote_ttl(c) for c in ("stocks", "etfs", "commodities", "forex")} == {60}


async def test_many_viewers_of_one_stock_share_one_observation(market, sources, session, prices):
    aapl = await session.get(Asset, await asset_id(session, "AAPL"))

    quotes = await asyncio.gather(*(market.quote(aapl) for _ in range(50)))

    assert sources["yahoo"].calls["fetch_ticker"] == 1
    assert len({q.observed_at for q in quotes}) == 1


# --- router ---------------------------------------------------------------------------


def make_router(clock: Clock | None = None):
    clock = clock or Clock()
    first, second = FakeSource("first", clock), FakeSource("second", clock)
    router = SourceRouter(
        {"first": first, "second": second}, {"crypto": ["first", "second"]}, clock.monotonic
    )
    ref = AssetRef("X", "crypto", {"first": "X/USDT", "second": "X/USD"})
    return router, first, second, ref, clock


async def test_asset_not_listed_on_the_first_source_falls_through():
    router, first, second, ref, _ = make_router()
    second.price("X/USD", 10)

    quote = await router.fetch_quote(ref)

    assert quote.source == "second" and quote.last == 10


async def test_first_source_in_the_chain_wins_when_it_lists_the_asset():
    router, first, second, ref, _ = make_router()
    first.price("X/USDT", 11)
    second.price("X/USD", 10)

    assert (await router.fetch_quote(ref)).source == "first"
    assert second.calls["fetch_ticker"] == 0


async def test_failing_source_falls_through_to_the_next():
    router, first, second, ref, _ = make_router()
    first.price("X/USDT", 11)
    second.price("X/USD", 10)
    first.failing = True

    assert (await router.fetch_quote(ref)).source == "second"


async def test_all_sources_failing_is_data_unavailable():
    router, first, second, ref, _ = make_router()
    first.price("X/USDT", 11)
    second.price("X/USD", 10)
    first.failing = second.failing = True

    with pytest.raises(DataUnavailable):
        await router.fetch_quote(ref)


async def test_asset_on_no_source_is_data_unavailable():
    router, _, _, ref, _ = make_router()

    with pytest.raises(DataUnavailable):
        await router.fetch_quote(ref)


async def test_three_failures_skip_the_source_for_sixty_seconds():
    router, first, second, ref, clock = make_router()
    first.price("X/USDT", 11)
    second.price("X/USD", 10)
    first.failing = True
    for _ in range(3):
        await router.fetch_quote(ref)
    assert first.calls["fetch_ticker"] == 3 and router.is_tripped("first")

    first.failing = False
    assert (await router.fetch_quote(ref)).source == "second"
    assert first.calls["fetch_ticker"] == 3  # not asked while tripped

    clock.advance(61)
    assert (await router.fetch_quote(ref)).source == "first"


async def test_a_success_resets_the_failure_count():
    router, first, second, ref, _ = make_router()
    first.price("X/USDT", 11)
    second.price("X/USD", 10)
    for failing in (True, True, False, True, True):
        first.failing = failing
        await router.fetch_quote(ref)

    assert not router.is_tripped("first")


async def test_a_slow_source_times_out_and_the_next_one_answers(monkeypatch):
    monkeypatch.setattr("app.marketdata.router.SOURCE_TIMEOUT_SECONDS", 0.01)
    router, first, second, ref, _ = make_router()
    first.price("X/USDT", 11)
    second.price("X/USD", 10)
    first.delay = 0.2

    assert (await router.fetch_quote(ref)).source == "second"


async def test_resolve_returns_the_first_listing_source():
    router, first, second, ref, _ = make_router()
    second.add("X/USD", currency="USD")

    name, instrument = await router.resolve(ref)

    assert name == "second" and instrument.symbol == "X/USD"


async def test_resolve_distinguishes_not_listed_from_unknown():
    router, first, second, ref, _ = make_router()
    assert await router.resolve(ref) is None

    first.failing = True
    with pytest.raises(DataUnavailable):
        await router.resolve(ref)


def test_chains_come_from_configuration():
    from app.config import Settings

    settings = Settings(chain_crypto="kraken, okx", chain_stocks="yahoo,stooq")

    assert settings.chains()["crypto"] == ["kraken", "okx"]
    assert settings.chains()["stocks"] == ["yahoo", "stooq"]


# --- calendars -------------------------------------------------------------------------


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_crypto_is_always_open():
    status = market_status("24/7", at("2026-03-07 03:00"))  # a Saturday night

    assert status.is_open and status.next_open is None


def test_us_stock_is_closed_on_saturday_with_the_next_open():
    status = market_status("XNYS", at("2026-03-07 15:00"))

    assert not status.is_open
    # Monday 9 March 09:30 New York; daylight saving started on the 8th.
    assert status.next_open == at("2026-03-09 13:30")


def test_us_stock_is_open_during_the_session_and_closed_after():
    assert market_status("XNAS", at("2026-03-04 15:00")).is_open
    assert not market_status("XNAS", at("2026-03-04 21:00")).is_open
    assert not market_status("XNAS", at("2026-03-04 14:29")).is_open


def test_us_stock_is_closed_on_an_exchange_holiday():
    status = market_status("XNYS", at("2026-07-03 15:00"))  # Friday, Independence Day observed

    assert not status.is_open
    assert status.next_open == at("2026-07-06 13:30")


@pytest.mark.parametrize(
    ("moment", "is_open"),
    [
        ("2026-03-06 21:59", True),  # Friday, just before the close
        ("2026-03-06 22:00", False),
        ("2026-03-07 12:00", False),  # Saturday
        ("2026-03-08 21:59", False),  # Sunday, just before the open
        ("2026-03-08 22:00", True),
        ("2026-03-04 03:00", True),
    ],
)
def test_forex_week_boundaries(moment, is_open):
    status = market_status("FOREX", at(moment))

    assert status.is_open is is_open
    if not is_open:
        assert status.next_open == at("2026-03-08 22:00")


def test_futures_follow_the_cme_session():
    assert market_status("CMES", at("2026-03-04 15:00")).is_open
    saturday = market_status("CMES", at("2026-03-07 15:00"))
    assert not saturday.is_open and saturday.next_open.weekday() == 6  # reopens on Sunday


def test_weekday_hours_calendar_for_exchanges_without_one():
    code = "WD|Europe/Vienna|09:00|17:30"

    assert market_status(code, at("2026-03-04 10:00")).is_open
    closed = market_status(code, at("2026-03-06 17:00"))  # Friday 18:00 local
    assert not closed.is_open and closed.next_open == at("2026-03-09 08:00")


# --- freshness --------------------------------------------------------------------------


NOW = at("2026-03-04 15:00")


def test_crypto_quote_five_seconds_old_is_fresh():
    fresh, delayed, age = classify("crypto", NOW - timedelta(seconds=5), NOW, True)

    assert fresh and not delayed and age == 5


def test_crypto_quote_older_than_thirty_seconds_is_stale():
    assert classify("crypto", NOW - timedelta(seconds=30), NOW, True)[0]
    assert not classify("crypto", NOW - timedelta(seconds=31), NOW, True)[0]


def test_stock_quote_fifteen_minutes_old_is_fresh_and_delayed():
    fresh, delayed, _ = classify("stocks", NOW - timedelta(minutes=15), NOW, True)

    assert fresh and delayed


def test_recent_stock_quote_is_not_labelled_delayed():
    fresh, delayed, _ = classify("stocks", NOW - timedelta(seconds=30), NOW, True)

    assert fresh and not delayed


@pytest.mark.parametrize("asset_class", ["stocks", "etfs", "commodities", "forex"])
def test_quote_older_than_twenty_minutes_is_stale(asset_class):
    assert classify(asset_class, NOW - timedelta(minutes=20), NOW, True)[0]
    assert not classify(asset_class, NOW - timedelta(minutes=20, seconds=1), NOW, True)[0]


def test_quote_for_a_closed_market_is_stale_regardless_of_age():
    fresh, delayed, _ = classify("stocks", NOW, NOW, False)

    assert not fresh and not delayed


# --- conversion rates --------------------------------------------------------------------


async def test_usd_to_eur_is_derived_from_the_eurusd_quote(market, prices, clock):
    rate = await market.fx("USD", "EUR")

    assert rate.rate == Decimal(1) / Decimal("1.25") == Decimal("0.8")
    assert rate.source == "yahoo" and rate.observed_at == clock()


async def test_eur_to_usd_is_the_eurusd_quote(market, prices):
    assert (await market.fx("EUR", "USD")).rate == Decimal("1.25")


async def test_same_currency_rate_is_exactly_one(market, sources):
    rate = await market.fx("EUR", "EUR")

    assert rate.rate == 1 and rate.source is None
    assert sources["yahoo"].calls["fetch_ticker"] == 0


async def test_usdt_is_treated_as_usd(market, prices, sources):
    assert (await market.fx("USDT", "USD")).rate == 1
    assert (await market.fx("USDT", "EUR")).rate == Decimal("0.8")


async def test_other_currencies_use_their_own_pair(market, sources):
    sources["yahoo"].price("JPYEUR=X", "0.0056")

    assert (await market.fx("JPY", "EUR")).rate == Decimal("0.0056")


async def test_sub_units_are_scaled(market, sources):
    sources["yahoo"].price("GBPUSD=X", "1.30")

    assert (await market.fx("GBp", "USD")).rate == Decimal("0.0130")
    assert (await market.fx("USX", "USD")).rate == Decimal("0.01")


async def test_missing_rate_is_data_unavailable(market):
    with pytest.raises(DataUnavailable):
        await market.fx("USD", "EUR")


# --- API -------------------------------------------------------------------------------


async def test_quote_endpoint_with_bid_and_ask(client, session, prices):
    btc = await asset_id(session, "BTC")

    body = (await client.get(f"/api/market/quote/{btc}")).json()

    assert (body["last"], body["bid"], body["ask"]) == ("50000", "49990", "50010")
    assert body["source"] == "okx" and body["currency"] == "USDT"
    assert body["last_price_only"] is False
    assert body["fresh"] is True and body["market_open"] is True and body["observed_at"]


async def test_quote_endpoint_marks_last_price_only(client, session, sources):
    sources["yahoo"].price("GC=F", 4200)
    gold = await asset_id(session, "GC=F")

    body = (await client.get(f"/api/market/quote/{gold}")).json()

    assert body["last_price_only"] is True and body["bid"] is None and body["ask"] is None


async def test_crypto_falls_back_to_the_next_source_in_the_chain(client, session, sources):
    sources["kraken"].price("BNB/USD", 600, bid=599, ask=601)
    bnb = await asset_id(session, "BNB")

    body = (await client.get(f"/api/market/quote/{bnb}")).json()

    assert body["source"] == "kraken"


async def test_stock_is_priced_from_yahoo_and_stale_when_closed(client, session, prices, clock):
    aapl = await asset_id(session, "AAPL")
    assert (await client.get(f"/api/market/quote/{aapl}")).json()["source"] == "yahoo"

    clock.set(at("2026-03-07 15:00"))  # Saturday
    body = (await client.get(f"/api/market/quote/{aapl}")).json()

    assert body["market_open"] is False and body["stale"] is True
    assert body["next_open"].startswith("2026-03-09T13:30")


async def test_delayed_stock_quote_is_fresh_and_labelled(client, session, sources, clock):
    sources["yahoo"].price("AAPL", 200, observed_at=clock() - timedelta(minutes=15))
    aapl = await asset_id(session, "AAPL")

    body = (await client.get(f"/api/market/quote/{aapl}")).json()

    assert body["fresh"] is True and body["delayed"] is True


async def test_quote_endpoint_reports_unavailable_data(client, session):
    btc = await asset_id(session, "BTC")

    response = await client.get(f"/api/market/quote/{btc}")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "data_unavailable"


async def test_batch_quotes_map_missing_data_to_null(client, session, prices):
    btc, eth = await asset_id(session, "BTC"), await asset_id(session, "ETH")

    body = (await client.get(f"/api/market/quotes?ids={btc},{eth}")).json()

    assert body[str(btc)]["last"] == "50000" and body[str(eth)] is None


async def test_market_status_endpoint(client, session, clock):
    aapl, btc = await asset_id(session, "AAPL"), await asset_id(session, "BTC")
    clock.set(at("2026-03-07 15:00"))

    stock = (await client.get(f"/api/market/status/{aapl}")).json()
    crypto = (await client.get(f"/api/market/status/{btc}")).json()

    assert stock["is_open"] is False and stock["next_open"].startswith("2026-03-09T13:30")
    assert crypto == {"asset_id": btc, "is_open": True, "next_open": None}


async def test_daily_history_is_ascending_with_one_bar_per_day(client, session, sources, clock):
    sources["yahoo"].add("AAPL", "equity", exchange="NMS")
    sources["yahoo"].bars[("AAPL", "1d")] = daily_bars(
        clock() - timedelta(days=9), list(range(100, 110))
    )
    aapl = await asset_id(session, "AAPL")

    body = (await client.get(f"/api/market/history/{aapl}?resolution=1d")).json()

    times = [bar["time"] for bar in body["bars"]]
    assert body["source"] == "yahoo" and len(times) == 10 and times == sorted(times)
    assert len({t[:10] for t in times}) == 10
    assert body["bars"][0]["close"] == "100"


async def test_history_before_the_first_bar_is_empty_not_an_error(client, session, sources, clock):
    sources["yahoo"].add("AAPL", "equity", exchange="NMS")
    sources["yahoo"].bars[("AAPL", "1d")] = daily_bars(clock() - timedelta(days=3), [1, 2, 3])
    aapl = await asset_id(session, "AAPL")

    response = await client.get(
        f"/api/market/history/{aapl}",
        params={"start": "2001-01-01T00:00:00Z", "end": "2001-02-01T00:00:00Z"},
    )

    assert response.status_code == 200 and response.json()["bars"] == []


async def test_hourly_history_defaults_to_thirty_days(client, session, sources, clock):
    sources["okx"].add("BTC/USDT", currency="USDT")
    sources["okx"].bars[("BTC/USDT", "1h")] = daily_bars(clock() - timedelta(days=45), [1] * 46)
    btc = await asset_id(session, "BTC")

    body = (await client.get(f"/api/market/history/{btc}?resolution=1h")).json()

    # Bars older than 30 days are outside the default range.
    assert len(body["bars"]) == 31 and body["resolution"] == "1h"
