import csv
import io
from decimal import Decimal

from fastapi import APIRouter, Depends, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import error, get_market, get_portfolio, get_session_factory
from app.api.schemas import OrderIn, OrderOut, PreviewIn, PreviewOut, TradeOut, _plain
from app.auth import current_user
from app.db import SessionFactory, get_session
from app.marketdata.service import MarketService
from app.models import Asset, Order, Portfolio, Trade
from app.trading import orders as service

router = APIRouter(
    prefix="/api/portfolios/{portfolio_id}", tags=["trading"], dependencies=[Depends(current_user)]
)


def _refused(exc: service.OrderRefused):
    detail = {k: _plain(v) if isinstance(v, Decimal) else v for k, v in exc.detail.items()}
    return error(
        status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message, **jsonable_encoder(detail)
    )


def _order_fields(order: Order, symbol: str, trade_id: int | None) -> OrderOut:
    return OrderOut(
        id=order.id,
        portfolio_id=order.portfolio_id,
        asset_id=order.asset_id,
        asset_symbol=symbol,
        client_order_id=order.client_order_id,
        side=order.side,
        type=order.type,
        quantity=order.quantity,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        status=order.status,
        reserved_cash=order.reserved_cash,
        reject_reason=order.reject_reason,
        created_at=order.created_at,
        closed_at=order.closed_at,
        trade_id=trade_id,
    )


def _trade_fields(trade: Trade, asset: Asset) -> TradeOut:
    return TradeOut(
        id=trade.id,
        order_id=trade.order_id,
        portfolio_id=trade.portfolio_id,
        asset_id=trade.asset_id,
        asset_symbol=asset.symbol,
        asset_class=asset.asset_class,
        executed_at=trade.executed_at,
        side=trade.side,
        quantity=trade.quantity,
        price=trade.price,
        quote_currency=trade.quote_currency,
        quote_source=trade.quote_source,
        quote_observed_at=trade.quote_observed_at,
        fx_rate=trade.fx_rate,
        value_base=trade.value_base,
        liquidity=trade.liquidity,
        fee=trade.fee,
        fee_profile_name=trade.fee_profile_name,
        fee_terms=trade.fee_terms,
        fee_rate_fallback=trade.fee_rate_fallback,
        cash_change=trade.cash_change,
        realized_pnl=trade.realized_pnl,
    )


async def _orders(session: AsyncSession, portfolio_id: int, order_id: int | None = None):
    query = (
        select(Order, Asset.symbol, Trade.id)
        .join(Asset, Asset.id == Order.asset_id)
        .outerjoin(Trade, Trade.order_id == Order.id)
        .where(Order.portfolio_id == portfolio_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
    )
    if order_id is not None:
        query = query.where(Order.id == order_id)
    # Orders change in other sessions (fills, cancellations); never serve a cached copy.
    await session.commit()
    rows = await session.execute(query.execution_options(populate_existing=True))
    return [_order_fields(order, symbol, trade_id) for order, symbol, trade_id in rows]


async def _trades(session: AsyncSession, portfolio_id: int, trade_id: int | None = None):
    query = (
        select(Trade, Asset)
        .join(Asset, Asset.id == Trade.asset_id)
        .where(Trade.portfolio_id == portfolio_id)
        .order_by(Trade.executed_at, Trade.id)
    )
    if trade_id is not None:
        query = query.where(Trade.id == trade_id)
    return [_trade_fields(trade, asset) for trade, asset in await session.execute(query)]


def _request(body: OrderIn | PreviewIn) -> service.OrderRequest:
    return service.OrderRequest(
        asset_id=body.asset_id,
        side=body.side,
        type=body.type,
        quantity=body.quantity,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        client_order_id=getattr(body, "client_order_id", ""),
    )


@router.post("/orders/preview", response_model=PreviewOut)
async def preview_order(
    body: PreviewIn,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    """Estimated price, fee and cash effect, computed with the rules an actual fill uses."""
    asset = await session.get(Asset, body.asset_id)
    if asset is None:
        raise error(status.HTTP_404_NOT_FOUND, "not_found", "Asset not found.")
    try:
        preview = await service.preview_order(session, market, portfolio, asset, _request(body))
    except service.OrderRefused as exc:
        raise _refused(exc) from None
    return PreviewOut(
        estimated_price=preview.estimated_price,
        price_currency=preview.price_currency,
        fx_rate=preview.fx_rate,
        liquidity=preview.liquidity,
        fills_now=preview.fills_now,
        value_base=preview.amounts.value_base,
        fee=preview.amounts.fee,
        cash_change=preview.amounts.cash_change,
        base_currency=portfolio.base_currency,
        fee_profile_name=preview.fee_profile_name,
        fee_rate_fallback=preview.fee_rate_fallback,
        quote_source=preview.quote_source,
        quote_observed_at=preview.quote_observed_at,
        available_cash=preview.available_cash,
    )


@router.post("/orders", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def place_order(
    body: OrderIn,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
    session_factory: SessionFactory = Depends(get_session_factory),
    market: MarketService = Depends(get_market),
):
    try:
        order = await service.place_order(session_factory, market, portfolio.id, _request(body))
    except service.OrderRefused as exc:
        raise _refused(exc) from None
    return (await _orders(session, portfolio.id, order.id))[0]


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    portfolio: Portfolio = Depends(get_portfolio), session: AsyncSession = Depends(get_session)
):
    return await _orders(session, portfolio.id)


@router.post("/orders/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(
    order_id: int,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    found = await _orders(session, portfolio.id, order_id)
    if not found:
        raise error(status.HTTP_404_NOT_FOUND, "not_found", "Order not found.")
    if not await service.cancel_order(session_factory, order_id):
        current = (await _orders(session, portfolio.id, order_id))[0]
        raise error(
            status.HTTP_409_CONFLICT,
            "not_open",
            f"The order is {current.status} and can no longer be cancelled.",
        )
    return (await _orders(session, portfolio.id, order_id))[0]


@router.get("/trades", response_model=list[TradeOut])
async def list_trades(
    portfolio: Portfolio = Depends(get_portfolio), session: AsyncSession = Depends(get_session)
):
    return await _trades(session, portfolio.id)


CSV_COLUMNS = (
    "executed_at",
    "asset_symbol",
    "asset_class",
    "side",
    "quantity",
    "price",
    "quote_currency",
    "quote_source",
    "quote_observed_at",
    "fx_rate",
    "value_base",
    "liquidity",
    "fee",
    "fee_profile_name",
    "cash_change",
    "realized_pnl",
)


@router.get("/trades.csv")
async def trades_csv(
    portfolio: Portfolio = Depends(get_portfolio), session: AsyncSession = Depends(get_session)
):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for trade in await _trades(session, portfolio.id):
        row = trade.model_dump(mode="json")
        writer.writerow([row[column] for column in CSV_COLUMNS])
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="trades-{portfolio.id}.csv"'},
    )


@router.get("/trades/{trade_id}", response_model=TradeOut)
async def trade_detail(
    trade_id: int,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
):
    found = await _trades(session, portfolio.id, trade_id)
    if not found:
        raise error(status.HTTP_404_NOT_FOUND, "not_found", "Trade not found.")
    return found[0]
