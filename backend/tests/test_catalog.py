from collections import Counter
from decimal import Decimal

from sqlalchemy import select

from app import catalog
from app.catalog import STABLECOINS, check_all_assets, check_asset, load_seed, seed_assets
from app.config import ASSET_CLASSES
from app.marketdata.calendars import is_known_calendar
from app.models import Asset
from tests.conftest import asset_id, create_portfolio, place


def test_seed_file_has_ten_assets_in_each_of_five_classes():
    entries = load_seed()

    assert Counter(entry["asset_class"] for entry in entries) == dict.fromkeys(ASSET_CLASSES, 10)
    assert len({entry["symbol"] for entry in entries}) == 50


def test_seed_file_has_no_stablecoins():
    crypto = {entry["symbol"] for entry in load_seed() if entry["asset_class"] == "crypto"}

    assert not crypto & STABLECOINS


def test_seed_entries_are_complete():
    for entry in load_seed():
        assert entry["name"] and entry["symbols"], entry["symbol"]
        assert Decimal(entry["quantity_step"]) > 0
        assert is_known_calendar(entry["calendar"]), entry["symbol"]


async def test_fresh_installation_shows_ten_assets_per_class(client):
    classes = (await client.get("/api/assets/classes")).json()
    assert classes == [{"asset_class": c, "count": 10} for c in ASSET_CLASSES]

    for asset_class in ASSET_CLASSES:
        assets = (await client.get(f"/api/assets?asset_class={asset_class}")).json()
        assert len(assets) == 10
        assert [a["rank"] for a in assets] == list(range(1, 11))


async def test_seeding_twice_keeps_fifty_assets_with_the_same_ids(session):
    before = {a.symbol: a.id for a in (await session.scalars(select(Asset))).all()}

    await seed_assets(session)
    await seed_assets(session)

    after = {a.symbol: a.id for a in (await session.scalars(select(Asset))).all()}
    assert len(after) == 50 and after == before


async def test_crypto_metadata_comes_from_the_exchange(client, session, market, prices):
    btc = await session.get(Asset, await asset_id(session, "BTC"))
    await check_asset(session, market, btc)

    body = (await client.get(f"/api/assets/{btc.id}")).json()

    assert body["source"] == "okx" and body["available"] is True
    assert body["quote_currency"] == "USDT"
    assert body["quantity_step"] == "0.00000001" and body["min_order_size"] == "0.00001"
    assert body["calendar"] == "24/7"


async def test_stock_metadata(client, session, market, prices):
    aapl = await session.get(Asset, await asset_id(session, "AAPL"))
    await check_asset(session, market, aapl)

    body = (await client.get(f"/api/assets/{aapl.id}")).json()

    assert body["quote_currency"] == "USD" and body["quantity_step"] == "1"
    assert body["calendar"] == "XNAS" and body["source"] == "yahoo"
    assert body["name"] == "Apple Inc." and body["asset_class"] == "stocks"


async def test_asset_missing_on_the_preferred_source_resolves_to_a_fallback(
    session, market, sources
):
    # BNB is not listed on the first exchange in the chain, only on the second.
    sources["kraken"].add("BNB/USD", currency="USD", step="0.0001", minimum="0.01")
    bnb = await session.get(Asset, await asset_id(session, "BNB"))

    await check_asset(session, market, bnb)

    assert bnb.available and bnb.resolved_source == "kraken"
    assert bnb.quote_currency == "USD" and bnb.quantity_step == Decimal("0.0001")


async def test_asset_on_no_source_is_unavailable_and_not_tradable(
    client, session, session_factory, market, prices
):
    await check_all_assets(session_factory, market)

    eth = await session.get(Asset, await asset_id(session, "ETH"), populate_existing=True)
    btc = await session.get(Asset, await asset_id(session, "BTC"), populate_existing=True)
    assert not eth.available and eth.resolved_source is None
    assert btc.available

    # Still visible, labelled unavailable, and refused for trading and tracking.
    listed = (await client.get("/api/assets?asset_class=crypto")).json()
    assert {a["symbol"]: a["available"] for a in listed}["ETH"] is False
    portfolio = await create_portfolio(client)
    response = await place(client, portfolio["id"], asset_id=eth.id, quantity=1)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "asset_unavailable"
    tracked = await client.post(
        f"/api/portfolios/{portfolio['id']}/assets", json={"asset_id": eth.id}
    )
    assert tracked.status_code == 422


async def test_a_failing_source_does_not_mark_assets_unavailable(session, market, sources):
    for source in sources.values():
        source.failing = True
    btc = await session.get(Asset, await asset_id(session, "BTC"))

    await check_asset(session, market, btc)

    assert btc.available


async def test_search_finds_instruments_beyond_the_seed(client, sources):
    sources["yahoo"].add("SAP.DE", "equity", "EUR", name="SAP SE", exchange="GER")
    sources["okx"].add("PEPE/USDT", currency="USDT", base="PEPE", name="PEPE")

    stocks = (await client.get("/api/assets/search?q=sap")).json()
    coins = (await client.get("/api/assets/search?q=pepe")).json()

    assert stocks == [
        {
            "source": "yahoo",
            "symbol": "SAP.DE",
            "name": "SAP SE",
            "instrument_type": "equity",
            "asset_class": "stocks",
            "supported": True,
            "exchange": "GER",
            "asset_id": None,
        }
    ]
    assert [(c["source"], c["symbol"], c["asset_class"]) for c in coins] == [
        ("okx", "PEPE/USDT", "crypto")
    ]


async def test_adding_a_found_asset_puts_it_in_the_reported_class(client, sources):
    sources["yahoo"].add("SAP.DE", "equity", "EUR", name="SAP SE", exchange="GER")

    response = await client.post("/api/assets", json={"source": "yahoo", "symbol": "SAP.DE"})

    assert response.status_code == 201
    asset = response.json()
    assert asset["asset_class"] == "stocks" and asset["symbol"] == "SAP.DE"
    assert asset["quote_currency"] == "EUR" and asset["calendar"] == "XETR"
    assert asset["available"] is True and asset["seeded"] is False
    assert asset["quantity_step"] == "1"

    # Everyone sees it, and it can be added to a portfolio.
    stocks = (await client.get("/api/assets?asset_class=stocks")).json()
    assert len(stocks) == 11 and stocks[-1]["symbol"] == "SAP.DE"
    portfolio = await create_portfolio(client)
    tracked = await client.post(
        f"/api/portfolios/{portfolio['id']}/assets", json={"asset_id": asset["id"]}
    )
    assert tracked.status_code == 201
    found = (await client.get("/api/assets/search?q=sap")).json()
    assert found[0]["asset_id"] == asset["id"]


async def test_adding_the_same_asset_twice_returns_the_existing_one(client, sources):
    sources["yahoo"].add("SAP.DE", "equity", "EUR", exchange="GER")

    first = (await client.post("/api/assets", json={"source": "yahoo", "symbol": "SAP.DE"})).json()
    second = (await client.post("/api/assets", json={"source": "yahoo", "symbol": "SAP.DE"})).json()

    assert first["id"] == second["id"]


async def test_added_coin_gets_symbols_on_every_source_that_lists_it(client, session, sources):
    sources["okx"].add("PEPE/USDT", currency="USDT", base="PEPE", step="1", minimum="1000")
    sources["kraken"].add("PEPE/USD", currency="USD", base="PEPE")

    asset = (await client.post("/api/assets", json={"source": "okx", "symbol": "PEPE/USDT"})).json()

    stored = await session.get(Asset, asset["id"])
    assert asset["symbol"] == "PEPE" and asset["asset_class"] == "crypto"
    assert {link.source: link.symbol for link in stored.source_symbols} == {
        "okx": "PEPE/USDT",
        "kraken": "PEPE/USD",
    }
    assert asset["min_order_size"] == "1000" and asset["calendar"] == "24/7"


async def test_search_without_a_match_is_empty_and_changes_nothing(client, session):
    response = await client.get("/api/assets/search?q=zzzznope")

    assert response.status_code == 200 and response.json() == []
    assert len((await session.scalars(select(Asset))).all()) == 50


async def test_unsupported_instrument_type_cannot_be_added(client, session, sources):
    sources["yahoo"].add("VFIAX", "mutualfund", name="Vanguard 500 Index Admiral", exchange="NAS")

    found = (await client.get("/api/assets/search?q=vfiax")).json()
    response = await client.post("/api/assets", json={"source": "yahoo", "symbol": "VFIAX"})

    assert found[0]["supported"] is False and found[0]["asset_class"] is None
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_instrument"
    assert "not supported" in response.json()["detail"]["message"]
    assert len((await session.scalars(select(Asset))).all()) == 50


async def test_unknown_symbol_cannot_be_added(client):
    response = await client.post("/api/assets", json={"source": "yahoo", "symbol": "NOPE"})

    assert response.status_code == 404


async def test_stablecoins_are_left_out_of_search(client, sources):
    sources["okx"].add("USDC/USDT", currency="USDT", base="USDC")

    assert (await client.get("/api/assets/search?q=usdc")).json() == []


async def test_price_recorder_keeps_the_last_price_and_source(session, market, prices, app):
    btc = await session.get(Asset, await asset_id(session, "BTC"))

    await market.quote(btc)

    await session.refresh(btc)
    assert btc.last_price == 50000 and btc.resolved_source == "okx"
    assert catalog.PRICE_RECORD_INTERVAL_SECONDS == 60
