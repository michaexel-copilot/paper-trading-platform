"""Portfolios: creation, management, tracked assets and fee profile selection."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ASSET_CLASSES, BASE_CURRENCIES
from app.dbtypes import utcnow
from app.feeprofiles import default_profiles
from app.models import (
    Asset,
    FeeProfile,
    Order,
    Portfolio,
    PortfolioAsset,
    PortfolioFeeProfile,
    Position,
    Trade,
    ValueSnapshot,
)


class PortfolioError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def create_portfolio(
    session: AsyncSession,
    user_id: int,
    name: str,
    base_currency: str,
    starting_cash: Decimal,
    now: datetime | None = None,
) -> Portfolio:
    name = name.strip()
    if not name:
        raise PortfolioError("invalid_name", "A portfolio needs a name.")
    if base_currency not in BASE_CURRENCIES:
        raise PortfolioError("invalid_currency", "Base currency must be EUR or USD.")
    if starting_cash <= 0:
        raise PortfolioError("invalid_cash", "Starting cash must be greater than zero.")
    now = now or utcnow()

    portfolio = Portfolio(
        user_id=user_id,
        name=name,
        base_currency=base_currency,
        starting_cash=starting_cash,
        cash=starting_cash,
        created_at=now,
    )
    session.add(portfolio)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise PortfolioError("name_taken", f"You already have a portfolio named '{name}'.") from exc

    for asset_class, profile in (await default_profiles(session)).items():
        session.add(
            PortfolioFeeProfile(
                portfolio_id=portfolio.id, asset_class=asset_class, fee_profile_id=profile.id
            )
        )
    session.add(
        ValueSnapshot(portfolio_id=portfolio.id, at=now, value=starting_cash, kind="created")
    )
    await session.commit()
    return portfolio


async def owned_portfolio(
    session: AsyncSession, user_id: int, portfolio_id: int
) -> Portfolio | None:
    """The portfolio if it belongs to the user; None both when missing and when not theirs."""
    return await session.scalar(
        select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
    )


async def list_portfolios(session: AsyncSession, user_id: int) -> list[Portfolio]:
    return list(
        await session.scalars(
            select(Portfolio).where(Portfolio.user_id == user_id).order_by(Portfolio.created_at)
        )
    )


async def rename_portfolio(session: AsyncSession, portfolio: Portfolio, name: str) -> Portfolio:
    name = name.strip()
    if not name:
        raise PortfolioError("invalid_name", "A portfolio needs a name.")
    portfolio.name = name
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise PortfolioError("name_taken", f"You already have a portfolio named '{name}'.") from exc
    return portfolio


async def delete_portfolio(session: AsyncSession, portfolio: Portfolio) -> None:
    """Remove the portfolio with everything in it. Open orders go too, so none can fill."""
    for model in (Trade, Order, Position, ValueSnapshot, PortfolioAsset, PortfolioFeeProfile):
        await session.execute(delete(model).where(model.portfolio_id == portfolio.id))
    await session.delete(portfolio)
    await session.commit()


async def tracked_assets(session: AsyncSession, portfolio_id: int) -> list[Asset]:
    return list(
        await session.scalars(
            select(Asset)
            .join(PortfolioAsset, PortfolioAsset.asset_id == Asset.id)
            .where(PortfolioAsset.portfolio_id == portfolio_id)
            .order_by(Asset.asset_class, Asset.symbol)
        )
    )


async def add_asset(session: AsyncSession, portfolio: Portfolio, asset_id: int) -> Asset:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise PortfolioError("unknown_asset", "This asset is not in the catalog.")
    if not asset.available:
        raise PortfolioError(
            "asset_unavailable", f"{asset.symbol} is unavailable: no source can price it."
        )
    exists = await session.scalar(
        select(PortfolioAsset.id).where(
            PortfolioAsset.portfolio_id == portfolio.id, PortfolioAsset.asset_id == asset_id
        )
    )
    if exists is None:
        session.add(PortfolioAsset(portfolio_id=portfolio.id, asset_id=asset_id))
        await session.commit()
    return asset


async def remove_asset(session: AsyncSession, portfolio: Portfolio, asset_id: int) -> None:
    held = await session.scalar(
        select(Position.id).where(
            Position.portfolio_id == portfolio.id, Position.asset_id == asset_id
        )
    )
    if held is not None:
        raise PortfolioError(
            "position_open", "Close the position before removing this asset from the portfolio."
        )
    open_order = await session.scalar(
        select(Order.id).where(
            Order.portfolio_id == portfolio.id,
            Order.asset_id == asset_id,
            Order.status == "open",
        )
    )
    if open_order is not None:
        raise PortfolioError(
            "order_open", "Cancel the open orders for this asset before removing it."
        )
    await session.execute(
        delete(PortfolioAsset).where(
            PortfolioAsset.portfolio_id == portfolio.id, PortfolioAsset.asset_id == asset_id
        )
    )
    await session.commit()


async def fee_profile_selection(session: AsyncSession, portfolio_id: int) -> dict[str, FeeProfile]:
    rows = await session.execute(
        select(PortfolioFeeProfile.asset_class, FeeProfile)
        .join(FeeProfile, FeeProfile.id == PortfolioFeeProfile.fee_profile_id)
        .where(PortfolioFeeProfile.portfolio_id == portfolio_id)
    )
    chosen = {asset_class: profile for asset_class, profile in rows}
    defaults = await default_profiles(session)
    return {cls: chosen.get(cls) or defaults[cls] for cls in ASSET_CLASSES if cls in defaults}


async def select_fee_profile(
    session: AsyncSession, portfolio: Portfolio, asset_class: str, profile_key: str
) -> FeeProfile:
    """Choose the profile for future fills of an asset class. Past trades are untouched."""
    if asset_class not in ASSET_CLASSES:
        raise PortfolioError("unknown_asset_class", f"Unknown asset class '{asset_class}'.")
    profile = await session.scalar(select(FeeProfile).where(FeeProfile.key == profile_key))
    if profile is None:
        raise PortfolioError("unknown_profile", f"Unknown fee profile '{profile_key}'.")
    if asset_class not in profile.asset_classes:
        raise PortfolioError(
            "profile_not_applicable", f"'{profile.name}' does not apply to {asset_class}."
        )
    row = await session.scalar(
        select(PortfolioFeeProfile).where(
            PortfolioFeeProfile.portfolio_id == portfolio.id,
            PortfolioFeeProfile.asset_class == asset_class,
        )
    )
    if row is None:
        session.add(
            PortfolioFeeProfile(
                portfolio_id=portfolio.id, asset_class=asset_class, fee_profile_id=profile.id
            )
        )
    else:
        row.fee_profile_id = profile.id
    await session.commit()
    return profile
