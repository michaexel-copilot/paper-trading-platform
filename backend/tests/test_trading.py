import asyncio
import csv
import io
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Asset, Order, Portfolio, Position, Trade, ValueSnapshot
from app.trading import engine
from app.trading.engine import FillPlan, Pricing, try_fill
from app.trading.fees import FeeTerms
from app.trading.matcher import AlreadyRunning, Matcher, ProcessLock
from tests.conftest import D, asset_id, create_portfolio, place
from tests.fakes import START


@pytest.fixture
async def aapl(session, prices) -> int:
    return await asset_id(session, "AAPL")


@pytest.fixture
async def btc(session, prices) -> int:
    return await asset_id(session, "BTC")


@pytest.fixture
async def portfolio(client) -> int:
    return (await create_portfolio(client, cash="10000"))["id"]


@pytest.fixture
def matcher(session_factory, market) -> Matcher:
    return Matcher(session_factory, market)


def reprice(prices, clock, symbol, last, bid=None, ask=None, source="yahoo"):
    """Change a quote and let the cached one expire."""
    prices[source].price(symbol, last, bid=bid, ask=ask)
    clock.advance(61)


async def detail(client, portfolio_id) -> dict:
    return (await client.get(f"/api/portfolios/{portfolio_id}")).json()


async def orders(client, portfolio_id) -> list[dict]:
    return (await client.get(f"/api/portfolios/{portfolio_id}/orders")).json()


async def trades(client, portfolio_id) -> list[dict]:
    return (await client.get(f"/api/portfolios/{portfolio_id}/trades")).json()


def refusal(response) -> dict:
    assert response.status_code == 422, response.text
    return response.json()["detail"]


# --- models ---------------------------------------------------------------------------


async def test_duplicate_client_order_id_is_rejected_by_the_database(
    session, client, portfolio, aapl
):
    def order():
        return Order(portfolio_id=portfolio, asset_id=aapl, client_order_id="same-id",
                     side="buy", type="market", quantity=D(1))  # fmt: skip

    session.add(order())
    await session.commit()
    session.add(order())
    with pytest.raises(IntegrityError):
        await session.commit()


# --- placing: validation ----------------------------------------------------------------


async def test_valid_market_order_is_accepted_and_filled(client, portfolio, aapl):
    response = await place(client, portfolio, asset_id=aapl, quantity=5)

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "filled" and order["asset_symbol"] == "AAPL"
    assert order["trade_id"] is not None and order["reserved_cash"] == "0"


async def test_quantity_must_be_on_the_step(client, portfolio, aapl):
    problem = refusal(await place(client, portfolio, asset_id=aapl, quantity="1.5"))

    assert problem["code"] == "quantity_step" and problem["quantity_step"] == "1"
    assert "multiple of 1" in problem["message"]


async def test_quantity_must_reach_the_minimum(client, session, market, portfolio, btc):
    from app.catalog import check_asset

    await check_asset(session, market, await session.get(Asset, btc))  # minimum 0.00001

    problem = refusal(await place(client, portfolio, asset_id=btc, quantity="0.000001"))

    assert problem["code"] == "below_minimum" and problem["min_order_size"] == "0.00001"


@pytest.mark.parametrize("quantity", ["0", "-1"])
async def test_quantity_must_be_positive(client, portfolio, aapl, quantity):
    assert refusal(await place(client, portfolio, asset_id=aapl, quantity=quantity))["code"] == (
        "invalid_quantity"
    )


async def test_limit_order_needs_a_limit_price(client, portfolio, aapl):
    problem = refusal(await place(client, portfolio, asset_id=aapl, type="limit", quantity=1))

    assert problem["code"] == "limit_price_required"


async def test_stop_order_needs_a_stop_price(client, portfolio, aapl):
    problem = refusal(await place(client, portfolio, asset_id=aapl, type="stop", quantity=1))

    assert problem["code"] == "stop_price_required"


async def test_unknown_asset_is_refused(client, portfolio, prices):
    assert refusal(await place(client, portfolio, asset_id=999999, quantity=1))["code"] == (
        "unknown_asset"
    )


async def test_refused_orders_leave_no_trace(client, portfolio, aapl):
    await place(client, portfolio, asset_id=aapl, quantity="1.5")
    await place(client, portfolio, asset_id=aapl, type="limit", quantity=1)

    assert await orders(client, portfolio) == []


# --- long-only, fully funded ---------------------------------------------------------------


async def test_buy_costing_more_than_available_cash_is_refused(client, portfolio, aapl):
    # 50 x 200.10 = 10005 plus a 1.00 fee against 10000 cash.
    problem = refusal(await place(client, portfolio, asset_id=aapl, quantity=50))

    assert problem["code"] == "insufficient_cash" and problem["shortfall"] == "6"
    assert "Short by 6.00" in problem["message"]


async def test_fee_counts_towards_the_cost(client, portfolio, aapl, prices, clock):
    reprice(prices, clock, "AAPL", 100, bid=100, ask=100)

    # 100 x 100 = exactly 10000; only the 1.00 fee is missing.
    problem = refusal(await place(client, portfolio, asset_id=aapl, quantity=100))

    assert problem["shortfall"] == "1"


async def test_cash_reserved_by_open_orders_is_not_available(client, portfolio, aapl):
    await place(client, portfolio, asset_id=aapl, type="limit", limit_price=150, quantity=40)

    problem = refusal(await place(client, portfolio, asset_id=aapl, quantity=25))

    assert problem["code"] == "insufficient_cash"


async def test_cannot_sell_more_than_held_minus_committed(client, portfolio, aapl):
    await place(client, portfolio, asset_id=aapl, quantity=2)
    await place(
        client, portfolio, asset_id=aapl, side="sell", type="limit", limit_price=300, quantity=1
    )

    problem = refusal(await place(client, portfolio, asset_id=aapl, side="sell", quantity=2))

    assert problem["code"] == "insufficient_holdings" and problem["available_quantity"] == "1"


async def test_short_selling_is_not_possible(client, portfolio, aapl):
    problem = refusal(await place(client, portfolio, asset_id=aapl, side="sell", quantity=1))

    assert problem["code"] == "insufficient_holdings"


# --- market orders --------------------------------------------------------------------------


async def test_market_buy_fills_at_the_ask(client, portfolio, aapl, prices, clock):
    reprice(prices, clock, "AAPL", 100, bid=99, ask=101)

    await place(client, portfolio, asset_id=aapl, quantity=1)

    trade = (await trades(client, portfolio))[0]
    assert trade["price"] == "101" and trade["liquidity"] == "taker"


async def test_market_sell_fills_at_the_bid(client, portfolio, aapl, prices, clock):
    reprice(prices, clock, "AAPL", 100, bid=99, ask=101)
    await place(client, portfolio, asset_id=aapl, quantity=1)

    await place(client, portfolio, asset_id=aapl, side="sell", quantity=1)

    assert (await trades(client, portfolio))[1]["price"] == "99"


async def test_market_order_on_a_last_price_only_quote_uses_the_assumed_spread(
    client, portfolio, aapl, prices, clock
):
    reprice(prices, clock, "AAPL", 100)

    await place(client, portfolio, asset_id=aapl, quantity=1)

    # The default stock profile assumes a spread of 0.02 %.
    assert (await trades(client, portfolio))[0]["price"] == "100.01"


async def test_market_order_is_refused_while_the_market_is_closed(client, portfolio, aapl, clock):
    clock.set(START + timedelta(days=3))  # Saturday

    problem = refusal(await place(client, portfolio, asset_id=aapl, quantity=1))

    assert problem["code"] == "market_closed"
    assert problem["next_open"].startswith("2026-03-09T13:30")
    assert await orders(client, portfolio) == []


async def test_market_order_is_refused_without_a_fresh_quote(
    client, portfolio, aapl, prices, clock
):
    prices["yahoo"].price("AAPL", 200, observed_at=clock() - timedelta(minutes=21))

    problem = refusal(await place(client, portfolio, asset_id=aapl, quantity=1))

    assert problem["code"] == "stale_quote"
    assert await trades(client, portfolio) == []


async def test_market_order_is_refused_without_any_quote(client, portfolio, aapl, prices):
    prices["yahoo"].failing = True

    assert refusal(await place(client, portfolio, asset_id=aapl, quantity=1))["code"] == "no_quote"


async def test_crypto_trades_at_the_weekend(client, portfolio, btc, clock):
    clock.set(START + timedelta(days=3))

    response = await place(client, portfolio, asset_id=btc, quantity="0.01")

    assert response.json()["status"] == "filled"


# --- limit orders ----------------------------------------------------------------------------


async def test_immediately_fillable_limit_buy_takes_the_ask(client, portfolio, btc, prices, clock):
    reprice(prices, clock, "BTC/USDT", 101, bid=100, ask=101, source="okx")

    order = (
        await place(client, portfolio, asset_id=btc, type="limit", limit_price=105, quantity=10)
    ).json()

    trade = (await trades(client, portfolio))[0]
    assert order["status"] == "filled"
    assert trade["price"] == "101" and trade["liquidity"] == "taker"
    assert trade["fee"] == "1.01"  # 1010 at the taker rate of 0.10 %


async def test_resting_limit_buy_fills_at_its_limit_as_maker(
    client, portfolio, btc, prices, clock, matcher
):
    reprice(prices, clock, "BTC/USDT", 100, bid=99, ask=100, source="okx")
    order = (
        await place(client, portfolio, asset_id=btc, type="limit", limit_price=95, quantity=10)
    ).json()
    assert order["status"] == "open"
    assert await matcher.tick() == 0

    reprice(prices, clock, "BTC/USDT", 94, bid=93, ask=94, source="okx")
    assert await matcher.tick() == 1

    trade = (await trades(client, portfolio))[0]
    assert trade["price"] == "95" and trade["liquidity"] == "maker"
    assert trade["fee"] == "0.76"  # 950 at the maker rate of 0.08 %
    assert (await orders(client, portfolio))[0]["status"] == "filled"


async def test_resting_limit_sell_fills_when_the_bid_reaches_it(
    client, portfolio, aapl, prices, clock, matcher
):
    await place(client, portfolio, asset_id=aapl, quantity=5)
    await place(
        client, portfolio, asset_id=aapl, side="sell", type="limit", limit_price=210, quantity=5
    )
    reprice(prices, clock, "AAPL", 209, bid=209, ask=209.2)
    assert await matcher.tick() == 0

    reprice(prices, clock, "AAPL", 211, bid=210.5, ask=211)
    assert await matcher.tick() == 1

    assert (await trades(client, portfolio))[1]["price"] == "210"


async def test_limit_order_can_be_placed_while_closed_and_stays_open(
    client, portfolio, aapl, prices, clock, matcher
):
    clock.set(START + timedelta(days=3))  # Saturday
    prices["yahoo"].price("AAPL", 90, bid=90, ask=90)  # beyond the limit, but the market is shut

    order = (
        await place(client, portfolio, asset_id=aapl, type="limit", limit_price=95, quantity=1)
    ).json()
    await matcher.tick()

    assert order["status"] == "open"
    assert (await orders(client, portfolio))[0]["status"] == "open"

    # Monday after the open, with a fresh quote at or below the limit, it fills.
    clock.set(START + timedelta(days=5))
    assert await matcher.tick() == 1


async def test_limit_order_does_not_fill_on_a_stale_quote(
    client, portfolio, aapl, prices, clock, matcher
):
    await place(client, portfolio, asset_id=aapl, type="limit", limit_price=95, quantity=1)
    prices["yahoo"].price("AAPL", 90, bid=90, ask=90, observed_at=clock() - timedelta(minutes=30))
    clock.advance(61)

    assert await matcher.tick() == 0


# --- stop orders -------------------------------------------------------------------------------


async def test_stop_loss_fills_at_the_bid_of_the_triggering_quote(
    client, portfolio, aapl, prices, clock, matcher
):
    await place(client, portfolio, asset_id=aapl, quantity=5)
    await place(
        client, portfolio, asset_id=aapl, side="sell", type="stop", stop_price=90, quantity=5
    )
    reprice(prices, clock, "AAPL", 91, bid=90.5, ask=91)
    assert await matcher.tick() == 0

    reprice(prices, clock, "AAPL", 89, bid=88.5, ask=89.5)
    assert await matcher.tick() == 1

    trade = (await trades(client, portfolio))[1]
    assert trade["price"] == "88.5" and trade["liquidity"] == "taker"


async def test_stop_fills_at_the_gap_price(client, portfolio, aapl, prices, clock, matcher):
    await place(client, portfolio, asset_id=aapl, quantity=5)
    await place(
        client, portfolio, asset_id=aapl, side="sell", type="stop", stop_price=90, quantity=5
    )
    clock.set(START + timedelta(days=5))  # the next Monday, after the open

    reprice(prices, clock, "AAPL", 80, bid=80, ask=80.5)
    assert await matcher.tick() == 1

    assert (await trades(client, portfolio))[1]["price"] == "80"


async def test_buy_stop_triggers_when_the_price_rises_to_it(
    client, portfolio, aapl, prices, clock, matcher
):
    await place(client, portfolio, asset_id=aapl, type="stop", stop_price=210, quantity=5)
    assert await matcher.tick() == 0

    reprice(prices, clock, "AAPL", 211, bid=210.8, ask=211.2)
    assert await matcher.tick() == 1

    assert (await trades(client, portfolio))[0]["price"] == "211.2"


async def test_buy_stop_costing_more_than_the_cash_is_rejected(
    client, portfolio, aapl, prices, clock, matcher
):
    # Reserves 45 x 210 + 1 = 9451 of the 10000.
    await place(client, portfolio, asset_id=aapl, type="stop", stop_price=210, quantity=45)

    reprice(prices, clock, "AAPL", 230, bid=229, ask=230)  # 45 x 230 = 10350
    assert await matcher.tick() == 0

    order = (await orders(client, portfolio))[0]
    assert order["status"] == "rejected" and order["reject_reason"] == "insufficient cash"
    state = await detail(client, portfolio)
    assert state["cash"] == "10000" and state["reserved_cash"] == "0"
    assert await trades(client, portfolio) == []


# --- reservations -----------------------------------------------------------------------------


async def test_cancelling_releases_the_reserved_cash(client, portfolio, aapl):
    order = (
        await place(client, portfolio, asset_id=aapl, type="limit", limit_price=100, quantity=20)
    ).json()
    assert order["reserved_cash"] == "2001"
    assert (await detail(client, portfolio))["available_cash"] == "7999"

    response = await client.post(f"/api/portfolios/{portfolio}/orders/{order['id']}/cancel")

    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert (await detail(client, portfolio))["available_cash"] == "10000"


async def test_filling_releases_the_reservation(client, portfolio, aapl, prices, clock, matcher):
    await place(client, portfolio, asset_id=aapl, type="limit", limit_price=100, quantity=20)

    reprice(prices, clock, "AAPL", 99, bid=99, ask=99.5)
    await matcher.tick()

    state = await detail(client, portfolio)
    assert state["reserved_cash"] == "0" and state["cash"] == state["available_cash"] == "7999"


async def test_open_sell_order_commits_its_quantity(client, portfolio, aapl):
    await place(client, portfolio, asset_id=aapl, quantity=5)

    await place(
        client, portfolio, asset_id=aapl, side="sell", type="limit", limit_price=300, quantity=3
    )

    assert (await detail(client, portfolio))["positions"][0]["committed"] == "3"


# --- evaluation of open orders -------------------------------------------------------------------


async def test_resting_order_fills_with_no_request_in_flight(
    app, client, portfolio, btc, prices, clock, monkeypatch
):
    """The matcher's own loop fills the order; nobody calls the API meanwhile."""
    monkeypatch.setattr("app.trading.matcher.TICK_SECONDS", 0.01)
    reprice(prices, clock, "BTC/USDT", 100, bid=99, ask=100, source="okx")
    await place(client, portfolio, asset_id=btc, type="limit", limit_price=95, quantity=10)
    await client.post("/api/auth/logout")

    reprice(prices, clock, "BTC/USDT", 94, bid=93, ask=94, source="okx")
    fill_time = clock()
    app.state.matcher.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            async with app.state.session_factory() as db:
                if await db.scalar(select(Trade.id)):
                    break
    finally:
        await app.state.matcher.stop()

    async with app.state.session_factory() as db:
        trade = await db.scalar(select(Trade))
    assert trade is not None and trade.executed_at == fill_time and trade.price == 95


def test_evaluation_intervals_are_within_the_limits():
    from app.marketdata.cache import quote_ttl
    from app.trading.matcher import TICK_SECONDS

    # Worst case between two evaluations against a new quote: tick plus cache lifetime.
    assert TICK_SECONDS + quote_ttl("crypto") <= 10
    assert quote_ttl("stocks") <= 60 and TICK_SECONDS <= 5


async def test_matcher_survives_a_failing_source(client, portfolio, aapl, prices, clock, matcher):
    await place(client, portfolio, asset_id=aapl, type="limit", limit_price=100, quantity=1)
    prices["yahoo"].failing = True
    clock.advance(61)

    assert await matcher.tick() == 0
    assert (await orders(client, portfolio))[0]["status"] == "open"


def test_only_one_process_may_hold_the_lock(tmp_path):
    first, second = ProcessLock(str(tmp_path / "lock")), ProcessLock(str(tmp_path / "lock"))
    first.acquire()
    with pytest.raises(AlreadyRunning):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


# --- currency conversion ------------------------------------------------------------------------


async def test_usd_asset_bought_in_a_eur_portfolio(client, session, prices, clock, aapl):
    eur = (await create_portfolio(client, "Euro", "EUR", "10000"))["id"]
    prices["yahoo"].price("EURUSD=X", D(1) / D("0.9"))  # 1 USD = 0.90 EUR
    reprice(prices, clock, "AAPL", 100, bid=100, ask=100)
    await client.put(f"/api/portfolios/{eur}/fee-profiles/stocks", json={"profile_key": "zero"})

    await place(client, eur, asset_id=aapl, quantity=10)

    trade = (await trades(client, eur))[0]
    assert D(trade["fx_rate"]).quantize(D("0.0001")) == D("0.9000")
    assert trade["value_base"] == "900" and trade["cash_change"] == "-900"
    assert (await detail(client, eur))["cash"] == "9100"


async def test_fee_is_added_to_the_converted_cost(client, prices, clock, aapl):
    eur = (await create_portfolio(client, "Euro", "EUR", "10000"))["id"]
    reprice(prices, clock, "AAPL", 100, bid=100, ask=100)  # EURUSD 1.25 -> 0.8 EUR per USD

    await place(client, eur, asset_id=aapl, quantity=10)

    trade = (await trades(client, eur))[0]
    assert trade["value_base"] == "800" and trade["fee"] == "0.8"
    assert trade["cash_change"] == "-800.8" and trade["fx_rate"] == "0.8"


# --- positions ----------------------------------------------------------------------------------


async def test_two_purchases_average_their_cost(client, portfolio, aapl, prices, clock):
    reprice(prices, clock, "AAPL", 100, bid=100, ask=100)
    await place(client, portfolio, asset_id=aapl, quantity=1)
    reprice(prices, clock, "AAPL", 200, bid=200, ask=200)
    await place(client, portfolio, asset_id=aapl, quantity=1)

    position = (await detail(client, portfolio))["positions"][0]

    assert position["quantity"] == "2" and position["avg_cost"] == "150"


async def test_partial_sale_keeps_the_average_cost_and_realises_profit(
    client, portfolio, aapl, prices, clock
):
    reprice(prices, clock, "AAPL", 100, bid=100, ask=100)
    await place(client, portfolio, asset_id=aapl, quantity=1)
    reprice(prices, clock, "AAPL", 200, bid=200, ask=200)
    await place(client, portfolio, asset_id=aapl, quantity=1)
    reprice(prices, clock, "AAPL", 180, bid=180, ask=180)

    await place(client, portfolio, asset_id=aapl, side="sell", quantity=1)

    state = await detail(client, portfolio)
    assert state["positions"][0]["quantity"] == "1"
    assert state["positions"][0]["avg_cost"] == "150"
    assert state["realized"] == "30"
    assert (await trades(client, portfolio))[2]["realized_pnl"] == "30"


async def test_selling_everything_closes_the_position(client, session, portfolio, aapl):
    await place(client, portfolio, asset_id=aapl, quantity=4)

    await place(client, portfolio, asset_id=aapl, side="sell", quantity=4)

    assert (await detail(client, portfolio))["positions"] == []
    assert (await session.scalars(select(Position))).all() == []


async def test_average_cost_excludes_fees(client, portfolio, aapl, prices, clock):
    reprice(prices, clock, "AAPL", 100, bid=100, ask=100)

    await place(client, portfolio, asset_id=aapl, quantity=10)  # fee 1.00

    assert (await detail(client, portfolio))["positions"][0]["avg_cost"] == "100"


# --- atomicity ---------------------------------------------------------------------------------


def plan_for(quote_price=100) -> FillPlan:
    from app.marketdata.base import Quote

    quote = Quote(D(quote_price), D(quote_price), D(quote_price), "USD", START, "yahoo")
    pricing = Pricing(FeeTerms("zero", "Zero commission"), D(1), D(1), D(1))
    return FillPlan(price=D(quote_price), liquidity="taker", quote=quote, pricing=pricing, marks={})


async def snapshot_state(session_factory, portfolio_id):
    async with session_factory() as db:
        portfolio = await db.get(Portfolio, portfolio_id)
        order = await db.scalar(select(Order))
        return (
            order.status,
            order.reserved_cash,
            portfolio.cash,
            portfolio.fees_paid,
            len((await db.scalars(select(Position))).all()),
            len((await db.scalars(select(Trade))).all()),
            len((await db.scalars(select(ValueSnapshot))).all()),
        )


async def test_failure_during_a_fill_changes_nothing(
    client, session_factory, portfolio, aapl, monkeypatch
):
    order = (
        await place(client, portfolio, asset_id=aapl, type="limit", limit_price=100, quantity=5)
    ).json()
    before = await snapshot_state(session_factory, portfolio)

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    # Fails after the order was claimed and cash and position were changed in memory.
    monkeypatch.setattr(engine, "_build_trade", explode)
    with pytest.raises(RuntimeError):
        await try_fill(session_factory, order["id"], plan_for(100))

    assert await snapshot_state(session_factory, portfolio) == before
    assert before[0] == "open"


async def test_two_concurrent_fills_of_one_order_produce_one_trade(
    client, session_factory, portfolio, aapl
):
    order = (
        await place(client, portfolio, asset_id=aapl, type="limit", limit_price=100, quantity=5)
    ).json()

    results = await asyncio.gather(
        *(try_fill(session_factory, order["id"], plan_for(100)) for _ in range(8))
    )

    assert sorted(r.status for r in results) == ["filled"] + ["skipped"] * 7
    state = await detail(client, portfolio)
    assert len(await trades(client, portfolio)) == 1
    assert state["cash"] == "9500" and state["positions"][0]["quantity"] == "5"


async def test_cash_never_goes_negative_under_concurrent_fills(
    client, session_factory, portfolio, aapl
):
    placed = [
        (
            await place(
                client, portfolio, asset_id=aapl, type="limit", limit_price=30, quantity=100
            )
        ).json()
        for _ in range(3)
    ]

    # Each fill would cost 4000 at a price that gapped above the limit; only two fit in 10000.
    results = await asyncio.gather(
        *(try_fill(session_factory, order["id"], plan_for(40)) for order in placed)
    )

    assert sorted(r.status for r in results) == ["filled", "filled", "rejected"]
    assert (await detail(client, portfolio))["cash"] == "2000"


async def test_repeated_submission_creates_one_order(client, portfolio, aapl):
    first = await place(client, portfolio, asset_id=aapl, quantity=1, client_order_id="dbl-click-1")
    second = await place(
        client, portfolio, asset_id=aapl, quantity=1, client_order_id="dbl-click-1"
    )

    assert first.json()["id"] == second.json()["id"]
    assert len(await orders(client, portfolio)) == 1 and len(await trades(client, portfolio)) == 1


async def test_simultaneous_repeated_submission_creates_one_order(client, portfolio, aapl):
    responses = await asyncio.gather(
        *(
            place(client, portfolio, asset_id=aapl, quantity=1, client_order_id="dbl-click-2")
            for _ in range(5)
        )
    )

    assert {r.json()["id"] for r in responses} == {responses[0].json()["id"]}
    assert len(await trades(client, portfolio)) == 1


# --- lifecycle -----------------------------------------------------------------------------------


async def test_cancelled_order_can_no_longer_fill(client, portfolio, aapl, prices, clock, matcher):
    order = (
        await place(client, portfolio, asset_id=aapl, type="limit", limit_price=100, quantity=1)
    ).json()
    await client.post(f"/api/portfolios/{portfolio}/orders/{order['id']}/cancel")

    reprice(prices, clock, "AAPL", 90, bid=90, ask=90)
    assert await matcher.tick() == 0

    assert (await orders(client, portfolio))[0]["status"] == "cancelled"


@pytest.mark.parametrize("final", ["filled", "cancelled"])
async def test_terminal_orders_cannot_be_cancelled(client, portfolio, aapl, final):
    if final == "filled":
        order = (await place(client, portfolio, asset_id=aapl, quantity=1)).json()
    else:
        order = (
            await place(client, portfolio, asset_id=aapl, type="limit", limit_price=1, quantity=1)
        ).json()
        await client.post(f"/api/portfolios/{portfolio}/orders/{order['id']}/cancel")

    response = await client.post(f"/api/portfolios/{portfolio}/orders/{order['id']}/cancel")

    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_open"
    assert (await orders(client, portfolio))[0]["status"] == final


async def test_cancelling_an_unknown_order_is_not_found(client, portfolio):
    assert (
        await client.post(f"/api/portfolios/{portfolio}/orders/424242/cancel")
    ).status_code == 404


# --- preview --------------------------------------------------------------------------------------


async def test_preview_equals_the_fill_when_the_quote_is_unchanged(client, portfolio, btc):
    request = {"asset_id": btc, "side": "buy", "type": "market", "quantity": "0.1"}

    preview = (
        await client.post(f"/api/portfolios/{portfolio}/orders/preview", json=request)
    ).json()
    await place(client, portfolio, **request)

    trade = (await trades(client, portfolio))[0]
    assert preview["fills_now"] is True and preview["estimated_price"] == trade["price"] == "50010"
    assert preview["fee"] == trade["fee"] == "5"
    assert preview["cash_change"] == trade["cash_change"] == "-5006"
    assert preview["fee_profile_name"] == trade["fee_profile_name"] == "Exchange spot, base tier"
    assert preview["available_cash"] == "10000" and preview["base_currency"] == "USD"


async def test_preview_of_a_resting_limit_order(client, portfolio, btc):
    request = {
        "asset_id": btc,
        "side": "buy",
        "type": "limit",
        "quantity": "0.1",
        "limit_price": "40000",
    }

    preview = (
        await client.post(f"/api/portfolios/{portfolio}/orders/preview", json=request)
    ).json()

    assert preview["fills_now"] is False and preview["liquidity"] == "maker"
    assert preview["estimated_price"] == "40000" and preview["fee"] == "3.2"
    assert await orders(client, portfolio) == []


async def test_preview_refuses_what_an_order_would_refuse(client, portfolio, aapl):
    response = await client.post(
        f"/api/portfolios/{portfolio}/orders/preview",
        json={"asset_id": aapl, "side": "buy", "type": "market", "quantity": "500"},
    )

    assert refusal(response)["code"] == "insufficient_cash"


# --- history --------------------------------------------------------------------------------------


async def test_trade_shows_price_source_conversion_and_fee(client, portfolio, btc, clock):
    await place(client, portfolio, asset_id=btc, quantity="0.1")
    listed = (await trades(client, portfolio))[0]

    trade = (await client.get(f"/api/portfolios/{portfolio}/trades/{listed['id']}")).json()

    assert trade == listed
    assert trade["asset_symbol"] == "BTC" and trade["side"] == "buy" and trade["quantity"] == "0.1"
    assert trade["price"] == "50010" and trade["quote_currency"] == "USDT"
    assert trade["quote_source"] == "okx"
    assert (
        trade["quote_observed_at"]
        == trade["executed_at"]
        == clock().isoformat().replace("+00:00", "Z")
    )
    assert trade["fx_rate"] == "1" and trade["fee"] == "5"
    assert trade["fee_profile_name"] == "Exchange spot, base tier"
    assert trade["fee_terms"]["taker_rate"] == "0.0010"
    assert trade["cash_change"] == "-5006"


async def test_trades_cannot_be_edited(client, portfolio, btc):
    await place(client, portfolio, asset_id=btc, quantity="0.1")
    trade = (await trades(client, portfolio))[0]
    url = f"/api/portfolios/{portfolio}/trades/{trade['id']}"

    for method in ("put", "patch", "delete"):
        response = await getattr(client, method)(url)
        assert response.status_code == 405

    assert (await client.get(url)).json() == trade


async def test_orders_and_fills_are_kept(client, portfolio, aapl):
    await place(client, portfolio, asset_id=aapl, quantity=1)
    cancelled = (
        await place(client, portfolio, asset_id=aapl, type="limit", limit_price=1, quantity=1)
    ).json()
    await client.post(f"/api/portfolios/{portfolio}/orders/{cancelled['id']}/cancel")

    statuses = sorted(o["status"] for o in await orders(client, portfolio))

    assert statuses == ["cancelled", "filled"]


async def test_csv_export_has_a_header_and_one_row_per_fill_in_time_order(
    client, portfolio, aapl, btc, clock
):
    await place(client, portfolio, asset_id=aapl, quantity=1)
    clock.advance(10)
    await place(client, portfolio, asset_id=btc, quantity="0.01")
    clock.advance(10)
    await place(client, portfolio, asset_id=aapl, side="sell", quantity=1)

    response = await client.get(f"/api/portfolios/{portfolio}/trades.csv")

    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    assert len(rows) == 4
    header, *data = rows
    assert header[:6] == ["executed_at", "asset_symbol", "asset_class", "side", "quantity", "price"]
    assert [(r[1], r[3]) for r in data] == [("AAPL", "buy"), ("BTC", "buy"), ("AAPL", "sell")]
    assert [r[0] for r in data] == sorted(r[0] for r in data)
