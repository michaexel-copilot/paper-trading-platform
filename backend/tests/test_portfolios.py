from datetime import timedelta

from sqlalchemy import select

from app.models import Asset, Order, Portfolio, Position, Trade, User, ValueSnapshot
from app.trading.matcher import Matcher
from app.trading.valuation import value_portfolio
from tests.conftest import D, asset_id, create_portfolio, place
from tests.fakes import START

# --- create and manage ------------------------------------------------------------


async def test_new_portfolio_holds_only_its_starting_cash(client):
    created = await create_portfolio(client, "Momentum", "EUR", "10000")

    detail = (await client.get(f"/api/portfolios/{created['id']}")).json()

    assert detail["name"] == "Momentum" and detail["base_currency"] == "EUR"
    assert detail["cash"] == detail["available_cash"] == detail["total_value"] == "10000"
    assert detail["positions"] == [] and detail["tracked"] == []
    assert detail["total_return"] == "0" and detail["stale"] is False


async def test_duplicate_name_is_refused(client, other_client):
    await create_portfolio(client, "Momentum")

    response = await client.post(
        "/api/portfolios",
        json={"name": "Momentum", "base_currency": "USD", "starting_cash": "500"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "name_taken"
    # The name is only taken for this user.
    await create_portfolio(other_client, "Momentum")


async def test_invalid_starting_cash_and_currency_are_refused(client):
    for cash in ("0", "-5"):
        response = await client.post(
            "/api/portfolios", json={"name": "X", "base_currency": "USD", "starting_cash": cash}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_cash"

    response = await client.post(
        "/api/portfolios", json={"name": "X", "base_currency": "GBP", "starting_cash": "100"}
    )
    assert response.json()["detail"]["code"] == "invalid_currency"
    assert (await client.get("/api/portfolios")).json() == []


async def test_listing_shows_value_and_return(client, session, prices):
    portfolio = await create_portfolio(client, "A", "USD", "10000")
    await create_portfolio(client, "B", "EUR", "500")
    await place(client, portfolio["id"], asset_id=await asset_id(session, "AAPL"), quantity=10)

    listed = (await client.get("/api/portfolios")).json()

    assert [(p["name"], p["base_currency"]) for p in listed] == [("A", "USD"), ("B", "EUR")]
    # Bought 10 at 200.10 with a 1.00 fee; marked at the last price of 200.
    assert listed[0]["total_value"] == "9998" and listed[0]["total_return"] == "-2"
    assert listed[0]["total_return_pct"] == "-0.02"
    assert listed[1]["total_value"] == "500"


async def test_rename(client):
    portfolio = await create_portfolio(client, "Old")
    await create_portfolio(client, "Taken")
    base = f"/api/portfolios/{portfolio['id']}"

    assert (await client.patch(base, json={"name": "New"})).json()["name"] == "New"
    assert (await client.patch(base, json={"name": "Taken"})).status_code == 409


async def test_base_currency_and_starting_cash_cannot_be_changed(client):
    portfolio = await create_portfolio(client, "Fixed", "EUR", "1000")

    await client.patch(
        f"/api/portfolios/{portfolio['id']}",
        json={"name": "Fixed", "base_currency": "USD", "starting_cash": "5"},
    )

    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()
    assert detail["base_currency"] == "EUR" and detail["starting_cash"] == "1000"


async def test_delete_needs_confirmation_and_removes_everything(
    client, session, session_factory, market, prices
):
    portfolio = await create_portfolio(client)
    pid = portfolio["id"]
    aapl = await asset_id(session, "AAPL")
    await place(client, pid, asset_id=aapl, quantity=5)
    await place(client, pid, asset_id=aapl, type="limit", limit_price=150, quantity=5)

    assert (await client.delete(f"/api/portfolios/{pid}")).status_code == 400
    assert (await client.get(f"/api/portfolios/{pid}")).status_code == 200

    assert (await client.delete(f"/api/portfolios/{pid}?confirm=true")).status_code == 204

    assert (await client.get(f"/api/portfolios/{pid}")).status_code == 404
    for model in (Portfolio, Position, Order, Trade, ValueSnapshot):
        assert (await session.scalars(select(model))).all() == []
    # Its open limit order is gone, so nothing is left that could fill.
    prices["yahoo"].price("AAPL", 100, bid=100, ask=100)
    assert await Matcher(session_factory, market).tick() == 0


# --- ownership ------------------------------------------------------------------------


async def test_another_users_portfolio_looks_like_a_missing_one(
    client, other_client, session, prices
):
    portfolio = await create_portfolio(client)
    pid = portfolio["id"]
    aapl = await asset_id(session, "AAPL")
    order = (
        await place(client, pid, asset_id=aapl, type="limit", limit_price=1, quantity=1)
    ).json()
    missing = (await other_client.get("/api/portfolios/999999")).json()

    attempts = [
        await other_client.get(f"/api/portfolios/{pid}"),
        await other_client.patch(f"/api/portfolios/{pid}", json={"name": "Mine now"}),
        await other_client.delete(f"/api/portfolios/{pid}?confirm=true"),
        await place(other_client, pid, asset_id=aapl, quantity=1),
        await other_client.post(
            f"/api/portfolios/{pid}/orders/preview",
            json={"asset_id": aapl, "side": "buy", "type": "market", "quantity": "1"},
        ),
        await other_client.post(f"/api/portfolios/{pid}/orders/{order['id']}/cancel"),
        await other_client.post(f"/api/portfolios/{pid}/assets", json={"asset_id": aapl}),
        await other_client.get(f"/api/portfolios/{pid}/orders"),
        await other_client.get(f"/api/portfolios/{pid}/trades"),
        await other_client.get(f"/api/portfolios/{pid}/trades.csv"),
        await other_client.get(f"/api/portfolios/{pid}/history"),
        await other_client.get(f"/api/portfolios/{pid}/fee-profiles"),
        await other_client.put(
            f"/api/portfolios/{pid}/fee-profiles/stocks", json={"profile_key": "zero"}
        ),
    ]

    for response in attempts:
        assert response.status_code == 404
        assert response.json() == missing
    assert (await other_client.get("/api/portfolios")).json() == []
    # Nothing changed for the owner.
    detail = (await client.get(f"/api/portfolios/{pid}")).json()
    assert detail["name"] == "Main"
    assert len((await client.get(f"/api/portfolios/{pid}/orders")).json()) == 1


# --- tracked assets ------------------------------------------------------------------


async def test_added_asset_shows_its_quote_and_a_zero_position(client, session, prices):
    portfolio = await create_portfolio(client)
    btc = await asset_id(session, "BTC")

    response = await client.post(
        f"/api/portfolios/{portfolio['id']}/assets", json={"asset_id": btc}
    )

    assert response.status_code == 201
    tracked = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()["tracked"]
    assert len(tracked) == 1
    assert tracked[0]["asset"]["symbol"] == "BTC" and tracked[0]["position_quantity"] == "0"
    assert tracked[0]["quote"]["last"] == "50000"


async def test_asset_with_a_position_or_open_order_cannot_be_removed(client, session, prices):
    portfolio = await create_portfolio(client)
    base = f"/api/portfolios/{portfolio['id']}"
    aapl, btc = await asset_id(session, "AAPL"), await asset_id(session, "BTC")
    await place(client, portfolio["id"], asset_id=aapl, quantity=1)
    order = (
        await place(client, portfolio["id"], asset_id=btc, type="limit", limit_price=10, quantity=1)
    ).json()

    held = await client.delete(f"{base}/assets/{aapl}")
    pending = await client.delete(f"{base}/assets/{btc}")

    assert held.status_code == 409 and "Close the position" in held.json()["detail"]["message"]
    assert pending.status_code == 409 and pending.json()["detail"]["code"] == "order_open"

    await client.post(f"{base}/orders/{order['id']}/cancel")
    assert (await client.delete(f"{base}/assets/{btc}")).status_code == 204
    assert [t["asset"]["symbol"] for t in (await client.get(base)).json()["tracked"]] == ["AAPL"]


async def test_trading_an_untracked_asset_tracks_it(client, session, prices):
    portfolio = await create_portfolio(client)

    await place(client, portfolio["id"], asset_id=await asset_id(session, "AAPL"), quantity=1)

    tracked = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()["tracked"]
    assert [t["asset"]["symbol"] for t in tracked] == ["AAPL"]
    assert tracked[0]["position_quantity"] == "1"


async def test_closed_position_leaves_the_asset_tracked(client, session, prices):
    portfolio = await create_portfolio(client)
    aapl = await asset_id(session, "AAPL")
    await place(client, portfolio["id"], asset_id=aapl, quantity=3)

    await place(client, portfolio["id"], asset_id=aapl, side="sell", quantity=3)

    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()
    assert detail["positions"] == []
    assert [t["asset"]["symbol"] for t in detail["tracked"]] == ["AAPL"]


# --- cash ------------------------------------------------------------------------------


async def test_cash_falls_by_cost_plus_fee(client, session, prices):
    portfolio = await create_portfolio(client, cash="5000")
    prices["yahoo"].price("AAPL", 100, bid=100, ask=100)

    await place(client, portfolio["id"], asset_id=await asset_id(session, "AAPL"), quantity=10)

    # 10 x 100 = 1000 plus the 1.00 minimum commission.
    assert (await client.get(f"/api/portfolios/{portfolio['id']}")).json()["cash"] == "3999"


async def test_open_buy_order_reserves_cash(client, session, prices):
    portfolio = await create_portfolio(client, cash="5000")
    prices["yahoo"].price("AAPL", 200, bid=200, ask=200)

    await place(
        client,
        portfolio["id"],
        asset_id=await asset_id(session, "AAPL"),
        type="limit",
        limit_price=100,
        quantity=20,
    )

    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()
    assert detail["cash"] == "5000" and detail["reserved_cash"] == "2001"
    assert detail["available_cash"] == "2999"


# --- valuation -------------------------------------------------------------------------


async def test_usd_position_in_a_eur_portfolio(client, session, prices):
    portfolio = await create_portfolio(client, currency="EUR", cash="10000")
    prices["yahoo"].price("AAPL", 100, bid=100, ask=100)
    await place(client, portfolio["id"], asset_id=await asset_id(session, "AAPL"), quantity=10)

    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()

    position = detail["positions"][0]
    # 10 x 100 USD at 0.8 EUR per USD.
    assert position["market_value"] == "800" and position["price_base"] == "80"
    assert position["price"] == "100" and position["price_currency"] == "USD"
    assert position["avg_cost"] == "80" and position["unrealized"] == "0"
    # Cash: 10000 - 800 - 0.80 fee (1.00 USD minimum at 0.8).
    assert detail["cash"] == "9199.2" and detail["total_value"] == "9999.2"


async def test_closed_market_uses_the_last_price_and_marks_the_valuation_stale(
    client, session, prices, clock
):
    portfolio = await create_portfolio(client)
    await place(client, portfolio["id"], asset_id=await asset_id(session, "AAPL"), quantity=10)
    prices["yahoo"].price("AAPL", 210, observed_at=clock())

    clock.set(START + timedelta(days=3))  # Saturday
    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()

    assert detail["stale"] is True
    assert detail["oldest_quote_age_seconds"] == 3 * 24 * 3600
    assert detail["positions"][0]["stale"] is True
    assert detail["positions"][0]["market_value"] == "2100"


async def test_position_without_any_quote_uses_the_recorded_price(
    client, session, market, prices, clock
):
    portfolio = await create_portfolio(client)
    await place(client, portfolio["id"], asset_id=await asset_id(session, "AAPL"), quantity=10)

    prices["yahoo"].failing = True
    clock.advance(120)
    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()

    position = detail["positions"][0]
    assert position["unpriced"] is True and position["stale"] is True
    assert position["market_value"] == "2000" and detail["stale"] is True


async def test_recorded_price_survives_a_restart(session, market, prices, clock):
    """With an empty quote cache, the price stored with the asset is used."""
    aapl = await session.get(Asset, await asset_id(session, "AAPL"))
    await market.quote(aapl)  # records 200 in the database
    user = User(email="owner@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    portfolio = Portfolio(
        user_id=user.id, name="P", base_currency="USD", starting_cash=D(1000), cash=D(1000)
    )
    session.add(portfolio)
    await session.flush()
    session.add(
        Position(portfolio_id=portfolio.id, asset_id=aapl.id, quantity=D(2), avg_cost=D(150))
    )
    await session.commit()
    await session.refresh(aapl)

    prices["yahoo"].failing = True
    market._quotes._values.clear()
    valuation = await value_portfolio(session, market, portfolio)

    assert valuation.positions[0].mark.unpriced
    assert valuation.positions[0].market_value == 400 and valuation.total_value == 1400


# --- performance -----------------------------------------------------------------------


async def test_total_return_is_net_of_fees(client, session, prices):
    portfolio = await create_portfolio(client, cash="10000")
    aapl = await asset_id(session, "AAPL")
    await client.put(
        f"/api/portfolios/{portfolio['id']}/fee-profiles/stocks", json={"profile_key": "flat-fee"}
    )
    # The flat fee is 1.00 EUR = 1.25 USD per order.
    prices["yahoo"].price("AAPL", 100, bid=100, ask=100)
    for _ in range(40):
        await place(client, portfolio["id"], asset_id=aapl, quantity=1)
    prices["yahoo"].price("AAPL", "112.5")
    prices["yahoo"].clock.advance(61)

    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()

    assert detail["fees_paid"] == "50"
    assert detail["unrealized"] == "500" and detail["realized"] == "0"
    assert detail["total_value"] == "10450"
    assert detail["total_return"] == "450" and detail["total_return_pct"] == "4.5"


# --- value history ---------------------------------------------------------------------


async def test_history_starts_at_the_starting_cash(client, clock):
    portfolio = await create_portfolio(client, cash="7500")

    history = (await client.get(f"/api/portfolios/{portfolio['id']}/history")).json()

    assert [(p["value"], p["kind"]) for p in history] == [("7500", "created")]


async def test_each_fill_records_a_point_at_the_fill_time(client, session, prices, clock):
    portfolio = await create_portfolio(client)
    clock.advance(600)
    await place(client, portfolio["id"], asset_id=await asset_id(session, "AAPL"), quantity=10)

    history = (await client.get(f"/api/portfolios/{portfolio['id']}/history")).json()
    trade = (await client.get(f"/api/portfolios/{portfolio['id']}/trades")).json()[0]

    assert [p["kind"] for p in history] == ["created", "fill"]
    assert history[1]["at"] == trade["executed_at"]
    assert history[1]["value"] == "9998"


async def test_one_daily_point_per_portfolio_per_day(
    client, session, session_factory, market, prices, clock
):
    first = await create_portfolio(client, "One", cash="1000")
    second = await create_portfolio(client, "Two", cash="2000")
    matcher = Matcher(session_factory, market)

    assert await matcher.daily_snapshots() == 2
    assert await matcher.daily_snapshots() == 0
    for day in range(1, 8):
        clock.set(START + timedelta(days=day))
        await matcher.daily_snapshots()
        await matcher.daily_snapshots()

    for portfolio, cash in ((first, "1000"), (second, "2000")):
        history = (await client.get(f"/api/portfolios/{portfolio['id']}/history")).json()
        daily = [p for p in history if p["kind"] == "daily"]
        assert history[0]["kind"] == "created" and history[0]["value"] == cash
        assert len(daily) == 8 and len({p["at"][:10] for p in daily}) == 8
