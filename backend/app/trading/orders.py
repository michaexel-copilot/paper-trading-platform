"""Placing, previewing, evaluating and cancelling orders."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.dbtypes import utcnow
from app.feeprofiles import profile_for, terms_for
from app.marketdata.base import DataUnavailable
from app.marketdata.service import MarketService, QuoteView
from app.models import Asset, Order, Portfolio, PortfolioAsset, Position
from app.trading.engine import (
    Amounts,
    FillPlan,
    FillResult,
    Pricing,
    amounts,
    decide,
    try_fill,
)
from app.trading.fees import ZERO, fill_price
from app.trading.valuation import marks_for_snapshot, open_order_holds

SIDES = ("buy", "sell")
ORDER_TYPES = ("market", "limit", "stop")


class OrderRefused(Exception):
    """The order cannot be accepted. ``code`` is stable; ``detail`` adds specifics."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True, slots=True)
class OrderRequest:
    asset_id: int
    side: str
    type: str
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str = ""


@dataclass(frozen=True, slots=True)
class Preview:
    estimated_price: Decimal
    price_currency: str
    fx_rate: Decimal
    liquidity: str
    fills_now: bool
    amounts: Amounts
    fee_profile_name: str
    fee_rate_fallback: bool
    quote_source: str | None
    quote_observed_at: datetime | None
    available_cash: Decimal


def validate(request: OrderRequest, asset: Asset) -> None:
    if request.side not in SIDES:
        raise OrderRefused("invalid_side", "Side must be buy or sell.")
    if request.type not in ORDER_TYPES:
        raise OrderRefused("invalid_type", "Order type must be market, limit or stop.")
    if not asset.available:
        raise OrderRefused("asset_unavailable", f"{asset.symbol} cannot be priced by any source.")
    if request.quantity <= 0:
        raise OrderRefused("invalid_quantity", "Quantity must be greater than zero.")
    if request.quantity % asset.quantity_step != 0:
        raise OrderRefused(
            "quantity_step",
            f"Quantity must be a multiple of {asset.quantity_step.normalize():f}.",
            quantity_step=asset.quantity_step,
        )
    if asset.min_order_size is not None and request.quantity < asset.min_order_size:
        raise OrderRefused(
            "below_minimum",
            f"The minimum order size is {asset.min_order_size.normalize():f}.",
            min_order_size=asset.min_order_size,
        )
    if request.type == "limit" and (request.limit_price is None or request.limit_price <= 0):
        raise OrderRefused("limit_price_required", "A limit order needs a positive limit price.")
    if request.type == "stop" and (request.stop_price is None or request.stop_price <= 0):
        raise OrderRefused("stop_price_required", "A stop order needs a positive stop price.")


async def _view_or_none(market: MarketService, asset: Asset) -> QuoteView | None:
    try:
        return await market.view(asset)
    except DataUnavailable:
        return None


async def pricing_for(
    session: AsyncSession,
    market: MarketService,
    portfolio: Portfolio,
    asset: Asset,
    view: QuoteView | None,
) -> Pricing:
    """Fee terms and conversion rates for a fill of this asset in this portfolio."""
    profile = await profile_for(session, portfolio.id, asset.asset_class)
    terms = terms_for(profile, view.quote.source if view else asset.resolved_source)
    quote_currency = view.quote.currency if view else asset.quote_currency
    fee_currency = terms.currency or quote_currency
    return Pricing(
        terms=terms,
        fx_rate=(await market.fx(quote_currency, portfolio.base_currency)).rate,
        quote_to_fee_currency=(await market.fx(quote_currency, fee_currency)).rate,
        fee_currency_to_base=(await market.fx(fee_currency, portfolio.base_currency)).rate,
    )


def _check_market_order(view: QuoteView | None) -> None:
    if view is None:
        raise OrderRefused("no_quote", "No price is available for this asset right now.")
    if not view.market_open:
        raise OrderRefused(
            "market_closed",
            "The market is closed. A market order can only be placed while it is open.",
            next_open=view.next_open,
        )
    if not view.fresh:
        raise OrderRefused(
            "stale_quote", "No fresh quote is available, so the order cannot be filled."
        )


def _estimate(
    request: OrderRequest, view: QuoteView | None, pricing: Pricing
) -> tuple[Decimal, str, bool, Amounts]:
    """Estimated price, liquidity, whether it fills now, and the resulting amounts."""
    probe = Order(
        side=request.side,
        type=request.type,
        quantity=request.quantity,
        limit_price=request.limit_price,
        stop_price=request.stop_price,
    )
    spread = pricing.terms.assumed_spread
    decision = decide(probe, view, spread, immediate=True) if view else None
    if decision is not None:
        price, liquidity = decision
        return (
            price,
            liquidity,
            True,
            amounts(request.side, price, request.quantity, liquidity, pricing),
        )
    if request.type == "market":
        price, liquidity = fill_price(view.quote, request.side, spread), "taker"
    elif request.type == "limit":
        price, liquidity = request.limit_price, "maker"
    else:
        price, liquidity = request.stop_price, "taker"
    return (
        price,
        liquidity,
        False,
        amounts(request.side, price, request.quantity, liquidity, pricing),
    )


def _reservation(request: OrderRequest, pricing: Pricing, estimate: Amounts) -> Decimal:
    """Cash an open buy order holds back: its estimated cost at the dearer fee rate."""
    if request.side != "buy":
        return ZERO
    costs = [-estimate.cash_change]
    if request.type == "limit":
        as_taker = amounts("buy", request.limit_price, request.quantity, "taker", pricing)
        costs.append(-as_taker.cash_change)
    return max(costs)


async def _check_funding(
    session: AsyncSession,
    portfolio: Portfolio,
    request: OrderRequest,
    needed_cash: Decimal,
) -> Decimal:
    """Refuse an order that is not fully funded. Returns the available cash."""
    reserved, committed = await open_order_holds(session, portfolio.id)
    available = portfolio.cash - reserved
    if request.side == "buy":
        if needed_cash > available:
            raise OrderRefused(
                "insufficient_cash",
                f"The order needs {needed_cash:.2f} {portfolio.base_currency} including the "
                f"fee; {available:.2f} is available. Short by {needed_cash - available:.2f}.",
                shortfall=needed_cash - available,
            )
        return available
    held = await session.scalar(
        select(Position.quantity).where(
            Position.portfolio_id == portfolio.id, Position.asset_id == request.asset_id
        )
    )
    free = (held or ZERO) - committed.get(request.asset_id, ZERO)
    if request.quantity > free:
        raise OrderRefused(
            "insufficient_holdings",
            f"Only {free.normalize():f} units are held and not committed to open sell orders.",
            available_quantity=free,
        )
    return available


async def _prepare(
    session: AsyncSession,
    market: MarketService,
    portfolio: Portfolio,
    asset: Asset,
    request: OrderRequest,
) -> tuple[QuoteView | None, Pricing, Preview, Decimal]:
    """Everything that is checked and computed the same way for a preview and an order."""
    validate(request, asset)
    view = await _view_or_none(market, asset)
    if request.type == "market":
        _check_market_order(view)
    try:
        pricing = await pricing_for(session, market, portfolio, asset, view)
    except DataUnavailable as exc:
        raise OrderRefused(
            "no_conversion_rate", "No currency conversion rate is available right now."
        ) from exc
    price, liquidity, fills_now, estimate = _estimate(request, view, pricing)
    reservation = _reservation(request, pricing, estimate)
    available = await _check_funding(session, portfolio, request, reservation)
    preview = Preview(
        estimated_price=price,
        price_currency=view.quote.currency if view else asset.quote_currency,
        fx_rate=pricing.fx_rate,
        liquidity=liquidity,
        fills_now=fills_now,
        amounts=estimate,
        fee_profile_name=pricing.terms.profile_name,
        fee_rate_fallback=pricing.terms.rate_fallback,
        quote_source=view.quote.source if view else None,
        quote_observed_at=view.quote.observed_at if view else None,
        available_cash=available,
    )
    return view, pricing, preview, reservation


async def preview_order(
    session: AsyncSession,
    market: MarketService,
    portfolio: Portfolio,
    asset: Asset,
    request: OrderRequest,
) -> Preview:
    _, _, preview, _ = await _prepare(session, market, portfolio, asset, request)
    return preview


async def evaluate(
    session_factory: SessionFactory, market: MarketService, order_id: int, *, immediate: bool
) -> FillResult | None:
    """Fill the order if the current quote allows it. Returns None when it does not."""
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        if order is None or order.status != "open":
            return None
        asset = await session.get(Asset, order.asset_id)
        portfolio = await session.get(Portfolio, order.portfolio_id)
        view = await _view_or_none(market, asset)
        if view is None or not (view.market_open and view.fresh):
            return None
        try:
            pricing = await pricing_for(session, market, portfolio, asset, view)
        except DataUnavailable:
            return None
        decision = decide(order, view, pricing.terms.assumed_spread, immediate=immediate)
        if decision is None:
            return None
        marks = await marks_for_snapshot(session, market, portfolio, also=asset)
    price, liquidity = decision
    plan = FillPlan(
        price=price, liquidity=liquidity, quote=view.quote, pricing=pricing, marks=marks
    )
    return await try_fill(session_factory, order_id, plan, market.now())


async def place_order(
    session_factory: SessionFactory,
    market: MarketService,
    portfolio_id: int,
    request: OrderRequest,
) -> Order:
    """Accept an order and fill it at once if the market allows. Raises OrderRefused."""
    async with session_factory() as session:
        duplicate = await _by_client_id(session, portfolio_id, request.client_order_id)
        if duplicate is not None:
            return duplicate
        portfolio = await session.get(Portfolio, portfolio_id)
        asset = await session.get(Asset, request.asset_id)
        if asset is None:
            raise OrderRefused("unknown_asset", "This asset is not in the catalog.")
        _, _, _, reservation = await _prepare(session, market, portfolio, asset, request)

        for attempt in (1, 2):
            tracked = await session.scalar(
                select(PortfolioAsset.id).where(
                    PortfolioAsset.portfolio_id == portfolio_id,
                    PortfolioAsset.asset_id == asset.id,
                )
            )
            order = Order(
                portfolio_id=portfolio_id,
                asset_id=asset.id,
                client_order_id=request.client_order_id,
                side=request.side,
                type=request.type,
                quantity=request.quantity,
                limit_price=request.limit_price if request.type == "limit" else None,
                stop_price=request.stop_price if request.type == "stop" else None,
                status="open",
                reserved_cash=reservation,
                created_at=market.now(),
            )
            session.add(order)
            if tracked is None:
                session.add(PortfolioAsset(portfolio_id=portfolio_id, asset_id=asset.id))
            try:
                await session.commit()
                break
            except IntegrityError:
                await session.rollback()
                # Either the same submission arrived twice at once and the other one
                # won, or another order started tracking the asset at the same moment.
                duplicate = await _by_client_id(session, portfolio_id, request.client_order_id)
                if duplicate is not None:
                    return duplicate
                if attempt == 2:
                    raise
        order_id = order.id

    result = await evaluate(session_factory, market, order_id, immediate=True)
    if request.type == "market" and result is None:
        # The quote went stale between the check and the fill; nothing may stay open.
        await _close(session_factory, order_id, "rejected", "no fresh quote at execution")

    async with session_factory() as session:
        return await session.get(Order, order_id)


async def _by_client_id(session: AsyncSession, portfolio_id: int, client_id: str) -> Order | None:
    return await session.scalar(
        select(Order).where(Order.portfolio_id == portfolio_id, Order.client_order_id == client_id)
    )


async def _close(
    session_factory: SessionFactory, order_id: int, status: str, reason: str | None = None
) -> bool:
    """Move an open order to a terminal status. False when it was not open any more."""
    async with session_factory() as session:
        changed = await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == "open")
            .values(status=status, reject_reason=reason, reserved_cash=ZERO, closed_at=utcnow())
        )
        await session.commit()
        return changed.rowcount == 1


async def cancel_order(session_factory: SessionFactory, order_id: int) -> bool:
    return await _close(session_factory, order_id, "cancelled")
