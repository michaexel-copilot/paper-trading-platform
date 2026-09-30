"""What a portfolio is worth and how it has performed."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.marketdata.base import DataUnavailable
from app.marketdata.service import MarketService
from app.models import Asset, Order, Portfolio, Position, Trade
from app.trading.fees import ZERO, round_money


@dataclass(frozen=True, slots=True)
class Mark:
    """The price used to value one unit of an asset."""

    price: Decimal | None  # in ``currency``; None when nothing was ever recorded
    currency: str
    price_base: Decimal | None  # in the portfolio's base currency
    stale: bool
    unpriced: bool  # no quote could be obtained; an earlier recorded price is used
    age_seconds: float | None
    source: str | None
    observed_at: datetime | None


@dataclass(slots=True)
class PositionValue:
    asset: Asset
    quantity: Decimal
    committed: Decimal
    avg_cost: Decimal
    mark: Mark
    market_value: Decimal
    unrealized: Decimal
    unrealized_pct: Decimal | None


@dataclass(slots=True)
class Valuation:
    cash: Decimal
    reserved_cash: Decimal
    available_cash: Decimal
    positions: list[PositionValue]
    total_value: Decimal
    stale: bool
    oldest_quote_age_seconds: float | None
    unrealized: Decimal
    realized: Decimal
    fees_paid: Decimal
    fees_by_class: dict[str, Decimal] = field(default_factory=dict)
    total_return: Decimal = ZERO
    total_return_pct: Decimal = ZERO


async def mark_asset(market: MarketService, asset: Asset, base_currency: str) -> Mark:
    """Latest price of the asset, converted to the base currency.

    Falls back to the last price seen, then to the last price stored with the
    asset, when no source answers.
    """
    price = observed_at = source = age = None
    currency = asset.quote_currency
    stale = unpriced = False
    try:
        view = await market.view(asset)
        quote, stale, age = view.quote, view.stale, view.age_seconds
    except DataUnavailable:
        quote, stale, unpriced = market.peek(asset), True, True
    if quote is not None:
        price, currency = quote.last, quote.currency
        observed_at, source = quote.observed_at, quote.source
    elif asset.last_price is not None:
        price, observed_at = asset.last_price, asset.last_price_at
    if age is None and observed_at is not None:
        age = max(0.0, (market.now() - observed_at).total_seconds())

    price_base = None
    if price is not None:
        try:
            price_base = price * (await market.fx(currency, base_currency)).rate
        except DataUnavailable:
            stale = unpriced = True
    return Mark(price, currency, price_base, stale, unpriced, age, source, observed_at)


async def open_order_holds(
    session: AsyncSession, portfolio_id: int
) -> tuple[Decimal, dict[int, Decimal]]:
    """Cash reserved by open buy orders and quantity committed by open sell orders."""
    reserved, committed = ZERO, {}
    orders = await session.scalars(
        select(Order).where(Order.portfolio_id == portfolio_id, Order.status == "open")
    )
    for order in orders:
        if order.side == "buy":
            reserved += order.reserved_cash
        else:
            committed[order.asset_id] = committed.get(order.asset_id, ZERO) + order.quantity
    return reserved, committed


async def fees_by_class(session: AsyncSession, portfolio_id: int) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    rows = await session.execute(
        select(Asset.asset_class, Trade.fee)
        .join(Asset, Asset.id == Trade.asset_id)
        .where(Trade.portfolio_id == portfolio_id)
    )
    for asset_class, fee in rows:
        totals[asset_class] = totals.get(asset_class, ZERO) + fee
    return totals


async def value_portfolio(
    session: AsyncSession, market: MarketService, portfolio: Portfolio
) -> Valuation:
    rows = (
        await session.execute(
            select(Position, Asset)
            .join(Asset, Asset.id == Position.asset_id)
            .where(Position.portfolio_id == portfolio.id)
            .order_by(Asset.symbol)
        )
    ).all()
    reserved, committed = await open_order_holds(session, portfolio.id)
    marks = await asyncio.gather(
        *(mark_asset(market, asset, portfolio.base_currency) for _, asset in rows)
    )

    positions, holdings_value, unrealized_total = [], ZERO, ZERO
    for (position, asset), mark in zip(rows, marks, strict=True):
        cost = position.quantity * position.avg_cost
        # With no price at all, carry the position at cost instead of at zero.
        value = position.quantity * mark.price_base if mark.price_base is not None else cost
        unrealized = value - cost
        positions.append(
            PositionValue(
                asset=asset,
                quantity=position.quantity,
                committed=committed.get(asset.id, ZERO),
                avg_cost=position.avg_cost,
                mark=mark,
                market_value=round_money(value),
                unrealized=round_money(unrealized),
                unrealized_pct=(unrealized / cost * 100) if cost else None,
            )
        )
        holdings_value += value
        unrealized_total += unrealized

    total_value = round_money(portfolio.cash + holdings_value)
    ages = [m.age_seconds for m in marks if m.age_seconds is not None]
    total_return = total_value - portfolio.starting_cash
    return Valuation(
        cash=portfolio.cash,
        reserved_cash=reserved,
        available_cash=portfolio.cash - reserved,
        positions=positions,
        total_value=total_value,
        stale=any(m.stale for m in marks),
        oldest_quote_age_seconds=max(ages) if ages else None,
        unrealized=round_money(unrealized_total),
        realized=portfolio.realized_pnl,
        fees_paid=portfolio.fees_paid,
        fees_by_class=await fees_by_class(session, portfolio.id),
        total_return=total_return,
        total_return_pct=total_return / portfolio.starting_cash * 100,
    )


async def marks_for_snapshot(
    session: AsyncSession, market: MarketService, portfolio: Portfolio, also: Asset | None = None
) -> dict[int, Decimal]:
    """Base-currency price per held asset, gathered before a fill's transaction starts."""
    assets = list(
        await session.scalars(
            select(Asset)
            .join(Position, Position.asset_id == Asset.id)
            .where(Position.portfolio_id == portfolio.id)
        )
    )
    if also is not None and all(asset.id != also.id for asset in assets):
        assets.append(also)
    marks = await asyncio.gather(
        *(mark_asset(market, asset, portfolio.base_currency) for asset in assets)
    )
    return {
        asset.id: mark.price_base
        for asset, mark in zip(assets, marks, strict=True)
        if mark.price_base is not None
    }
