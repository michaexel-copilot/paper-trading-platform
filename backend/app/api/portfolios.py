import asyncio

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import portfolios as service
from app.api.deps import error, get_market, get_portfolio
from app.api.schemas import (
    AssetOut,
    FeeProfileOut,
    FeeSelectionIn,
    PortfolioDetail,
    PortfolioIn,
    PortfolioRename,
    PortfolioSummary,
    PositionOut,
    QuoteOut,
    SnapshotOut,
    TrackAssetIn,
    TrackedAssetOut,
)
from app.auth import current_user
from app.db import get_session
from app.marketdata.base import DataUnavailable
from app.marketdata.service import MarketService
from app.models import FeeProfile, Portfolio, User, ValueSnapshot
from app.trading.fees import ZERO
from app.trading.valuation import Valuation, value_portfolio

router = APIRouter(prefix="/api", tags=["portfolios"], dependencies=[Depends(current_user)])

_STATUS_BY_CODE = {
    "name_taken": status.HTTP_409_CONFLICT,
    "position_open": status.HTTP_409_CONFLICT,
    "order_open": status.HTTP_409_CONFLICT,
    "unknown_asset": status.HTTP_404_NOT_FOUND,
    "unknown_profile": status.HTTP_404_NOT_FOUND,
}


def _http(exc: service.PortfolioError):
    code = _STATUS_BY_CODE.get(exc.code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    return error(code, exc.code, exc.message)


def _summary_fields(portfolio: Portfolio, valuation: Valuation) -> dict:
    return {
        "id": portfolio.id,
        "name": portfolio.name,
        "base_currency": portfolio.base_currency,
        "starting_cash": portfolio.starting_cash,
        "created_at": portfolio.created_at,
        "total_value": valuation.total_value,
        "total_return": valuation.total_return,
        "total_return_pct": valuation.total_return_pct,
        "stale": valuation.stale,
    }


@router.get("/portfolios", response_model=list[PortfolioSummary])
async def list_portfolios(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    result = []
    for portfolio in await service.list_portfolios(session, user.id):
        valuation = await value_portfolio(session, market, portfolio)
        result.append(PortfolioSummary(**_summary_fields(portfolio, valuation)))
    return result


@router.post("/portfolios", response_model=PortfolioSummary, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    body: PortfolioIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    try:
        portfolio = await service.create_portfolio(
            session, user.id, body.name, body.base_currency, body.starting_cash, market.now()
        )
    except service.PortfolioError as exc:
        raise _http(exc) from None
    valuation = await value_portfolio(session, market, portfolio)
    return PortfolioSummary(**_summary_fields(portfolio, valuation))


@router.get("/portfolios/{portfolio_id}", response_model=PortfolioDetail)
async def portfolio_detail(
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    valuation = await value_portfolio(session, market, portfolio)
    tracked = await service.tracked_assets(session, portfolio.id)

    async def quote_of(asset) -> QuoteOut | None:
        try:
            return QuoteOut.of(asset.id, await market.view(asset))
        except DataUnavailable:
            return None

    quotes = await asyncio.gather(*(quote_of(asset) for asset in tracked))
    held = {p.asset.id: p.quantity for p in valuation.positions}
    return PortfolioDetail(
        **_summary_fields(portfolio, valuation),
        cash=valuation.cash,
        reserved_cash=valuation.reserved_cash,
        available_cash=valuation.available_cash,
        realized=valuation.realized,
        unrealized=valuation.unrealized,
        fees_paid=valuation.fees_paid,
        fees_by_class=valuation.fees_by_class,
        oldest_quote_age_seconds=valuation.oldest_quote_age_seconds,
        positions=[
            PositionOut(
                asset=AssetOut.of(p.asset),
                quantity=p.quantity,
                committed=p.committed,
                avg_cost=p.avg_cost,
                price=p.mark.price,
                price_currency=p.mark.currency,
                price_base=p.mark.price_base,
                market_value=p.market_value,
                unrealized=p.unrealized,
                unrealized_pct=p.unrealized_pct,
                stale=p.mark.stale,
                unpriced=p.mark.unpriced,
                quote_source=p.mark.source,
                quote_observed_at=p.mark.observed_at,
            )
            for p in valuation.positions
        ],
        tracked=[
            TrackedAssetOut(
                asset=AssetOut.of(asset),
                quote=quote,
                position_quantity=held.get(asset.id, ZERO),
            )
            for asset, quote in zip(tracked, quotes, strict=True)
        ],
    )


@router.patch("/portfolios/{portfolio_id}", response_model=PortfolioSummary)
async def rename_portfolio(
    body: PortfolioRename,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    try:
        portfolio = await service.rename_portfolio(session, portfolio, body.name)
    except service.PortfolioError as exc:
        raise _http(exc) from None
    valuation = await value_portfolio(session, market, portfolio)
    return PortfolioSummary(**_summary_fields(portfolio, valuation))


@router.delete("/portfolios/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(
    confirm: bool = False,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
):
    """Deletes the portfolio with its positions, orders, trades and history."""
    if not confirm:
        raise error(
            status.HTTP_400_BAD_REQUEST,
            "confirmation_required",
            "Deleting a portfolio is permanent. Repeat the request with confirm=true.",
        )
    await service.delete_portfolio(session, portfolio)


@router.post(
    "/portfolios/{portfolio_id}/assets",
    response_model=AssetOut,
    status_code=status.HTTP_201_CREATED,
)
async def track_asset(
    body: TrackAssetIn,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
):
    try:
        return AssetOut.of(await service.add_asset(session, portfolio, body.asset_id))
    except service.PortfolioError as exc:
        raise _http(exc) from None


@router.delete(
    "/portfolios/{portfolio_id}/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def untrack_asset(
    asset_id: int,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
):
    try:
        await service.remove_asset(session, portfolio, asset_id)
    except service.PortfolioError as exc:
        raise _http(exc) from None


@router.get("/portfolios/{portfolio_id}/history", response_model=list[SnapshotOut])
async def value_history(
    portfolio: Portfolio = Depends(get_portfolio), session: AsyncSession = Depends(get_session)
):
    return (
        await session.scalars(
            select(ValueSnapshot)
            .where(ValueSnapshot.portfolio_id == portfolio.id)
            .order_by(ValueSnapshot.at, ValueSnapshot.id)
        )
    ).all()


# --- fee profiles ----------------------------------------------------------------


@router.get("/fee-profiles", response_model=list[FeeProfileOut])
async def fee_profiles(
    asset_class: str | None = None, session: AsyncSession = Depends(get_session)
):
    profiles = (await session.scalars(select(FeeProfile).order_by(FeeProfile.id))).all()
    if asset_class is not None:
        profiles = [p for p in profiles if asset_class in p.asset_classes]
    return profiles


@router.get("/portfolios/{portfolio_id}/fee-profiles", response_model=dict[str, FeeProfileOut])
async def portfolio_fee_profiles(
    portfolio: Portfolio = Depends(get_portfolio), session: AsyncSession = Depends(get_session)
):
    return await service.fee_profile_selection(session, portfolio.id)


@router.put("/portfolios/{portfolio_id}/fee-profiles/{asset_class}", response_model=FeeProfileOut)
async def choose_fee_profile(
    asset_class: str,
    body: FeeSelectionIn,
    portfolio: Portfolio = Depends(get_portfolio),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await service.select_fee_profile(session, portfolio, asset_class, body.profile_key)
    except service.PortfolioError as exc:
        raise _http(exc) from None
