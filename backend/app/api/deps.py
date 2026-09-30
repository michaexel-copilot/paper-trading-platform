from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.db import SessionFactory, get_session
from app.marketdata.service import MarketService
from app.models import Asset, Portfolio, User
from app.portfolios import owned_portfolio


def get_market(request: Request) -> MarketService:
    return request.app.state.market


def get_session_factory(request: Request) -> SessionFactory:
    return request.app.state.session_factory


def error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message, **extra})


async def get_asset(asset_id: int, session: AsyncSession = Depends(get_session)) -> Asset:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise error(status.HTTP_404_NOT_FOUND, "not_found", "Asset not found.")
    return asset


async def get_portfolio(
    portfolio_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Portfolio:
    """The caller's portfolio. Someone else's portfolio looks exactly like a missing one."""
    portfolio = await owned_portfolio(session, user.id, portfolio_id)
    if portfolio is None:
        raise error(status.HTTP_404_NOT_FOUND, "not_found", "Portfolio not found.")
    return portfolio
