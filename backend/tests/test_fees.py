from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import ASSET_CLASSES
from app.feeprofiles import default_profiles, load_seed, seed_fee_profiles, terms_for
from app.marketdata.base import Quote
from app.models import FeeProfile
from app.trading.fees import FeeTerms, compute_fee, fill_price
from tests.conftest import D, asset_id, create_portfolio, place
from tests.fakes import START


def terms(**charges) -> FeeTerms:
    return FeeTerms(
        profile_key="test", profile_name="Test", **{k: D(v) for k, v in charges.items()}
    )


# --- fee calculation: one case per scenario in the fee-models spec ------------------

FEE_CASES = [
    pytest.param(
        terms(taker_rate="0.001"), "2000", "1", "taker", "2.00", id="percentage fee"
    ),
    pytest.param(
        terms(per_unit="0.005", min_fee="1.00"), "2000", "10", "taker", "1.00", id="minimum applies"
    ),
    pytest.param(
        terms(per_unit="0.005", max_fee_rate="0.01"), "300", "1000", "taker", "3.00",
        id="maximum as a share of trade value applies",
    ),
    pytest.param(
        terms(maker_rate="0.0008", taker_rate="0.001"), "1000", "1", "maker", "0.80",
        id="maker rate for a resting limit order",
    ),
    pytest.param(
        terms(maker_rate="0.0008", taker_rate="0.001"), "1000", "1", "taker", "1.00",
        id="taker rate",
    ),
    pytest.param(
        terms(taker_rate="0.001", per_unit="0.01", fixed="1.50"), "1000", "100", "taker", "3.50",
        id="rate plus per-unit plus fixed",
    ),
    pytest.param(terms(fixed="9", max_fee="5"), "1000", "1", "taker", "5.00", id="absolute cap"),
    pytest.param(terms(taker_rate="0.001"), "1005", "1", "taker", "1.01", id="rounds half up"),
    pytest.param(terms(), "1000", "5", "taker", "0.00", id="zero commission"),
]  # fmt: skip


@pytest.mark.parametrize(("fee_terms", "value", "quantity", "liquidity", "expected"), FEE_CASES)
def test_fee_calculation(fee_terms, value, quantity, liquidity, expected):
    fee = compute_fee(fee_terms, value=D(value), quantity=D(quantity), liquidity=liquidity)

    assert fee == D(expected)


def test_fee_in_another_currency_is_converted_before_rounding():
    """Charges in USD, portfolio in EUR: compute in USD, convert, then round."""
    usd_terms = FeeTerms("ibkr", "IBKR", currency="USD", per_unit=D("0.005"), min_fee=D("1.00"))

    fee = compute_fee(
        usd_terms,
        value=D("30000"),
        quantity=D("333"),
        liquidity="taker",
        fee_currency_to_base=D("0.9"),
    )

    # 333 x 0.005 = 1.665 USD -> 1.4985 EUR -> 1.50; rounding first would give 1.51.
    assert fee == D("1.50")


def test_trade_value_is_converted_to_the_fee_currency_for_a_percentage_cap():
    eur_stock = FeeTerms(
        "ibkr", "IBKR", currency="USD", per_unit=D("0.005"), max_fee_rate=D("0.01")
    )

    fee = compute_fee(
        eur_stock,
        value=D("100"),  # EUR
        quantity=D("1000"),
        liquidity="taker",
        quote_to_fee_currency=D("1.25"),
        fee_currency_to_base=D("0.8"),
    )

    # Cap: 1 % of 125 USD = 1.25 USD -> 1.00 EUR.
    assert fee == D("1.00")


def test_fee_is_never_negative():
    rebate = terms(maker_rate="-0.0005")

    assert compute_fee(rebate, value=D(1000), quantity=D(1), liquidity="maker") == 0


# --- fill price ---------------------------------------------------------------------


def quote(last, bid=None, ask=None) -> Quote:
    return Quote(D(last), bid and D(bid), ask and D(ask), "USD", START, "test")


def test_buy_on_a_last_price_only_quote_pays_half_the_assumed_spread():
    assert fill_price(quote(100), "buy", D("0.001")) == D("100.05")


def test_sell_on_a_last_price_only_quote_gives_up_half_the_assumed_spread():
    assert fill_price(quote(100), "sell", D("0.001")) == D("99.95")


def test_assumed_spread_is_not_applied_when_bid_and_ask_exist():
    assert fill_price(quote(100, bid=99, ask=101), "buy", D("0.001")) == 101
    assert fill_price(quote(100, bid=99, ask=101), "sell", D("0.001")) == 99


# --- built-in profiles ----------------------------------------------------------------


async def test_every_asset_class_has_a_default_profile(session):
    defaults = await default_profiles(session)

    assert set(defaults) == set(ASSET_CLASSES)
    assert defaults["crypto"].key == "exchange-spot"
    assert defaults["stocks"].key == defaults["etfs"].key == "ibkr-fixed-us"
    for asset_class, profile in defaults.items():
        assert asset_class in profile.asset_classes


async def test_required_built_in_profiles_exist(session):
    profiles = {p.key: p for p in (await session.scalars(select(FeeProfile))).all()}

    assert {"exchange-spot", "ibkr-fixed-us", "flat-fee", "zero"} <= set(profiles)
    assert profiles["ibkr-fixed-us"].per_unit == D("0.005")
    assert profiles["flat-fee"].fixed == D("1.00") and profiles["flat-fee"].currency == "EUR"
    zero = profiles["zero"]
    assert set(zero.asset_classes) == set(ASSET_CLASSES)
    assert (zero.taker_rate, zero.per_unit, zero.fixed, zero.assumed_spread) == (0, 0, 0, 0)


def test_every_profile_records_where_and_when_it_was_checked():
    for entry in load_seed():
        assert entry.get("source_url") or entry.get("source_note"), entry["key"]
        assert isinstance(entry["checked_on"], date), entry["key"]
    exchange = next(e for e in load_seed() if e["key"] == "exchange-spot")
    for rates in exchange["exchange_rates"].values():
        assert rates["source_url"].startswith("https://")


async def test_seeding_profiles_again_updates_in_place(session):
    before = {p.key: p.id for p in (await session.scalars(select(FeeProfile))).all()}
    changed = load_seed()
    changed[0]["taker_rate"] = "0.002"

    await seed_fee_profiles(session, changed)

    after = (await session.scalars(select(FeeProfile))).all()
    assert {p.key: p.id for p in after} == before
    assert next(p for p in after if p.key == changed[0]["key"]).taker_rate == D("0.002")


# --- exchange-published crypto rates ---------------------------------------------------


async def test_crypto_is_charged_the_rates_of_the_pricing_exchange(session):
    profile = await session.scalar(select(FeeProfile).where(FeeProfile.key == "exchange-spot"))

    okx, kraken = terms_for(profile, "okx"), terms_for(profile, "kraken")

    assert (okx.maker_rate, okx.taker_rate, okx.rate_fallback) == (D("0.0008"), D("0.001"), False)
    assert (kraken.maker_rate, kraken.taker_rate) == (D("0.004"), D("0.008"))
    assert compute_fee(okx, value=D(1000), quantity=D(1), liquidity="taker") == D("1.00")


async def test_source_without_a_published_schedule_falls_back_to_the_profile(session):
    profile = await session.scalar(select(FeeProfile).where(FeeProfile.key == "exchange-spot"))

    yahoo = terms_for(profile, "yahoo")

    assert yahoo.rate_fallback is True
    assert (yahoo.maker_rate, yahoo.taker_rate) == (profile.maker_rate, profile.taker_rate)


async def test_profiles_without_exchange_rates_never_flag_a_fallback(session):
    profile = await session.scalar(select(FeeProfile).where(FeeProfile.key == "ibkr-fixed-us"))

    assert terms_for(profile, "yahoo").rate_fallback is False


async def test_trade_records_the_exchange_rate_and_the_fallback(client, session, sources, prices):
    sources["kraken"].price("ETH/USD", 1000, bid=1000, ask=1000)
    portfolio = await create_portfolio(client, cash="100000")
    btc, eth = await asset_id(session, "BTC"), await asset_id(session, "ETH")

    await place(client, portfolio["id"], asset_id=btc, quantity="0.02")  # okx, 1000.20
    await place(client, portfolio["id"], asset_id=eth, quantity=1)  # kraken
    sources["okx"].quotes.clear()
    sources["kraken"].quotes.clear()
    sources["yahoo"].price("XRP-USD", 2)
    xrp = await asset_id(session, "XRP")
    await place(client, portfolio["id"], asset_id=xrp, quantity=500)  # yahoo: no schedule

    trades = (await client.get(f"/api/portfolios/{portfolio['id']}/trades")).json()
    by_symbol = {t["asset_symbol"]: t for t in trades}
    assert by_symbol["BTC"]["fee"] == "1" and by_symbol["BTC"]["fee_rate_fallback"] is False
    assert by_symbol["ETH"]["fee"] == "8" and by_symbol["ETH"]["quote_source"] == "kraken"
    assert by_symbol["XRP"]["fee_rate_fallback"] is True and by_symbol["XRP"]["fee"] == "1"
    assert by_symbol["BTC"]["fee_terms"]["taker_rate"] == "0.0010"


# --- API: listing and selection ----------------------------------------------------------


async def test_listing_profiles_by_asset_class(client):
    profiles = (await client.get("/api/fee-profiles?asset_class=stocks")).json()

    assert {p["key"] for p in profiles} == {"ibkr-fixed-us", "flat-fee", "zero"}
    ibkr = next(p for p in profiles if p["key"] == "ibkr-fixed-us")
    assert ibkr["name"] == "Interactive Brokers Fixed (US)"
    assert (ibkr["per_unit"], ibkr["min_fee"], ibkr["max_fee_rate"]) == ("0.005", "1", "0.01")
    assert ibkr["source_url"].startswith("https://") and ibkr["checked_on"] == "2026-09-30"
    assert len((await client.get("/api/fee-profiles")).json()) == 6


async def test_new_portfolio_gets_the_default_profile_for_every_class(client):
    portfolio = await create_portfolio(client)

    chosen = (await client.get(f"/api/portfolios/{portfolio['id']}/fee-profiles")).json()

    assert set(chosen) == set(ASSET_CLASSES)
    assert chosen["crypto"]["key"] == "exchange-spot"


async def test_changing_a_profile_applies_to_later_fills_only(client, session, prices):
    portfolio = await create_portfolio(client)
    aapl = await asset_id(session, "AAPL")
    base = f"/api/portfolios/{portfolio['id']}"
    await place(client, portfolio["id"], asset_id=aapl, quantity=10)
    await place(client, portfolio["id"], asset_id=aapl, quantity=10)

    response = await client.put(f"{base}/fee-profiles/stocks", json={"profile_key": "zero"})
    assert response.status_code == 200 and response.json()["key"] == "zero"
    await place(client, portfolio["id"], asset_id=aapl, quantity=10)

    fees = [(t["fee"], t["fee_profile_name"]) for t in (await client.get(f"{base}/trades")).json()]
    assert fees == [
        ("1", "Interactive Brokers Fixed (US)"),
        ("1", "Interactive Brokers Fixed (US)"),
        ("0", "Zero commission"),
    ]


async def test_profile_must_apply_to_the_asset_class(client):
    portfolio = await create_portfolio(client)
    base = f"/api/portfolios/{portfolio['id']}/fee-profiles"

    wrong_class = await client.put(f"{base}/crypto", json={"profile_key": "ibkr-fixed-us"})
    unknown = await client.put(f"{base}/crypto", json={"profile_key": "nope"})

    assert wrong_class.status_code == 422 and unknown.status_code == 404


async def test_fee_report_per_asset_class_and_in_total(client, session, prices):
    portfolio = await create_portfolio(client, cash="100000")
    btc, aapl = await asset_id(session, "BTC"), await asset_id(session, "AAPL")
    await place(client, portfolio["id"], asset_id=btc, quantity="0.04")  # 2000.40 -> fee 2.00
    for _ in range(3):
        await place(client, portfolio["id"], asset_id=aapl, quantity=10)  # 1.00 each

    detail = (await client.get(f"/api/portfolios/{portfolio['id']}")).json()

    assert detail["fees_by_class"] == {"crypto": "2", "stocks": "3"}
    assert detail["fees_paid"] == "5"


def test_decimal_rates_survive_the_seed_file():
    okx = next(e for e in load_seed() if e["key"] == "exchange-spot")["exchange_rates"]["okx"]

    assert Decimal(okx["maker"]) == Decimal("0.0008") and Decimal(okx["taker"]) == Decimal("0.001")
