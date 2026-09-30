"""Order execution.

``decide`` says whether an order can fill against a quote and at what price;
``try_fill`` books the fill. Both are used when an order is placed and by the
background matcher, so the two paths cannot drift apart.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update

from app.db import SessionFactory
from app.dbtypes import utcnow
from app.marketdata.base import Quote
from app.marketdata.service import QuoteView
from app.models import Order, Portfolio, Position, Trade, ValueSnapshot
from app.trading.fees import ZERO, FeeTerms, compute_fee, fill_price, round_money


@dataclass(frozen=True, slots=True)
class Pricing:
    """Everything besides the price that turns a fill into cash amounts."""

    terms: FeeTerms
    fx_rate: Decimal  # quote currency -> portfolio base currency
    quote_to_fee_currency: Decimal
    fee_currency_to_base: Decimal


@dataclass(frozen=True, slots=True)
class Amounts:
    value_base: Decimal  # trade value in base currency, rounded to cents
    fee: Decimal
    cash_change: Decimal  # negative for a buy


@dataclass(frozen=True, slots=True)
class FillPlan:
    price: Decimal
    liquidity: str  # maker | taker
    quote: Quote
    pricing: Pricing
    # Base-currency prices of the held assets, for the value point recorded with the fill.
    marks: dict[int, Decimal]


@dataclass(frozen=True, slots=True)
class FillResult:
    status: str  # filled | rejected | skipped
    trade_id: int | None = None
    reason: str | None = None


def amounts(
    side: str, price: Decimal, quantity: Decimal, liquidity: str, pricing: Pricing
) -> Amounts:
    value = price * quantity
    value_base = round_money(value * pricing.fx_rate)
    fee = compute_fee(
        pricing.terms,
        value=value,
        quantity=quantity,
        liquidity=liquidity,
        quote_to_fee_currency=pricing.quote_to_fee_currency,
        fee_currency_to_base=pricing.fee_currency_to_base,
    )
    cash_change = -(value_base + fee) if side == "buy" else value_base - fee
    return Amounts(value_base, fee, cash_change)


def decide(
    order: Order, view: QuoteView, assumed_spread: Decimal, *, immediate: bool
) -> tuple[Decimal, str] | None:
    """Fill price and liquidity if the order can fill against this quote, else None.

    ``immediate`` is True when the order is evaluated as it is placed; a limit
    order filling then takes liquidity at the quote, while one that rested
    makes liquidity and fills at its limit price.
    """
    if not (view.market_open and view.fresh):
        return None
    quote = view.quote
    taking_price = fill_price(quote, order.side, assumed_spread)

    if order.type == "market":
        return taking_price, "taker"

    if order.type == "limit":
        reached = (
            taking_price <= order.limit_price
            if order.side == "buy"
            else taking_price >= order.limit_price
        )
        if not reached:
            return None
        return (taking_price, "taker") if immediate else (order.limit_price, "maker")

    if order.type == "stop":
        triggered = (
            quote.last >= order.stop_price
            if order.side == "buy"
            else quote.last <= order.stop_price
        )
        return (taking_price, "taker") if triggered else None

    return None


def _build_trade(order: Order, plan: FillPlan, result: Amounts, realized: Decimal, now: datetime):
    terms = plan.pricing.terms
    return Trade(
        order_id=order.id,
        portfolio_id=order.portfolio_id,
        asset_id=order.asset_id,
        executed_at=now,
        side=order.side,
        quantity=order.quantity,
        price=plan.price,
        quote_currency=plan.quote.currency,
        quote_source=plan.quote.source,
        quote_observed_at=plan.quote.observed_at,
        fx_rate=plan.pricing.fx_rate,
        value_base=result.value_base,
        liquidity=plan.liquidity,
        fee=result.fee,
        fee_profile_name=terms.profile_name,
        fee_terms=terms.as_record(),
        fee_rate_fallback=terms.rate_fallback,
        cash_change=result.cash_change,
        realized_pnl=realized,
    )


async def _reject(session, order_id: int, reason: str, now: datetime) -> FillResult:
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(status="rejected", reject_reason=reason, reserved_cash=ZERO, closed_at=now)
    )
    await session.commit()
    return FillResult("rejected", reason=reason)


async def try_fill(
    session_factory: SessionFactory, order_id: int, plan: FillPlan, now: datetime | None = None
) -> FillResult:
    """Book a fill in one transaction, or leave everything untouched.

    The first statement claims the order with a conditional update. That makes
    a second fill of the same order impossible and takes the database's write
    lock before any balance is read.
    """
    now = now or utcnow()
    async with session_factory() as session:
        claimed = await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == "open")
            .values(status="filled", reserved_cash=ZERO, closed_at=now)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            return FillResult("skipped", reason="order is no longer open")

        order = await session.get(Order, order_id, populate_existing=True)
        portfolio = await session.scalar(
            select(Portfolio).where(Portfolio.id == order.portfolio_id).with_for_update()
        )
        position = await session.scalar(
            select(Position).where(
                Position.portfolio_id == order.portfolio_id, Position.asset_id == order.asset_id
            )
        )
        result = amounts(order.side, plan.price, order.quantity, plan.liquidity, plan.pricing)
        held = position.quantity if position else ZERO

        if order.side == "sell" and order.quantity > held:
            return await _reject(session, order_id, "insufficient holdings", now)
        if portfolio.cash + result.cash_change < 0:
            return await _reject(session, order_id, "insufficient cash", now)

        realized = ZERO
        if order.side == "buy":
            # Average cost uses the unrounded trade value and excludes the fee.
            cost = plan.price * order.quantity * plan.pricing.fx_rate
            if position is None:
                position = Position(
                    portfolio_id=order.portfolio_id,
                    asset_id=order.asset_id,
                    quantity=ZERO,
                    avg_cost=ZERO,
                )
                session.add(position)
            total = held + order.quantity
            position.avg_cost = (held * position.avg_cost + cost) / total
            position.quantity = total
        else:
            realized = result.value_base - round_money(position.avg_cost * order.quantity)
            position.quantity = held - order.quantity
            if position.quantity == 0:
                await session.delete(position)

        portfolio.cash += result.cash_change
        portfolio.fees_paid += result.fee
        portfolio.realized_pnl += realized

        trade = _build_trade(order, plan, result, realized, now)
        session.add(trade)
        await session.flush()

        session.add(
            ValueSnapshot(
                portfolio_id=portfolio.id,
                at=now,
                value=await _value_after_fill(session, portfolio, plan.marks),
                kind="fill",
            )
        )
        await session.commit()
        return FillResult("filled", trade_id=trade.id)


async def _value_after_fill(session, portfolio: Portfolio, marks: dict[int, Decimal]) -> Decimal:
    positions = await session.scalars(select(Position).where(Position.portfolio_id == portfolio.id))
    holdings = sum(
        (p.quantity * marks.get(p.asset_id, p.avg_cost) for p in positions if p.quantity > 0),
        ZERO,
    )
    return round_money(portfolio.cash + holdings)
