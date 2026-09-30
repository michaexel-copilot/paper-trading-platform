from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog
from app.api.deps import error, get_asset, get_market
from app.api.schemas import AddAssetIn, AssetClassOut, AssetOut, SearchResultOut
from app.auth import current_user
from app.config import ASSET_CLASSES
from app.db import get_session
from app.marketdata.base import DataUnavailable, NotListed, SourceError
from app.marketdata.service import MarketService
from app.models import Asset

router = APIRouter(prefix="/api/assets", tags=["assets"], dependencies=[Depends(current_user)])


@router.get("/classes", response_model=list[AssetClassOut])
async def classes(session: AsyncSession = Depends(get_session)):
    rows = await session.execute(
        select(Asset.asset_class, func.count()).group_by(Asset.asset_class)
    )
    counts = {asset_class: count for asset_class, count in rows}
    return [AssetClassOut(asset_class=c, count=counts.get(c, 0)) for c in ASSET_CLASSES]


@router.get("", response_model=list[AssetOut])
async def list_assets(asset_class: str | None = None, session: AsyncSession = Depends(get_session)):
    query = select(Asset)
    if asset_class is not None:
        if asset_class not in ASSET_CLASSES:
            raise error(status.HTTP_404_NOT_FOUND, "not_found", "Unknown asset class.")
        query = query.where(Asset.asset_class == asset_class)
    # Seeded assets first in rank order, then assets added by search.
    assets = (await session.scalars(query)).all()
    assets = sorted(assets, key=lambda a: (a.asset_class, a.rank is None, a.rank or 0, a.symbol))
    return [AssetOut.of(asset) for asset in assets]


@router.get("/search", response_model=list[SearchResultOut])
async def search(
    q: str = Query(min_length=1, max_length=50),
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    return await catalog.search_instruments(session, market, q)


@router.post("", response_model=AssetOut, status_code=status.HTTP_201_CREATED)
async def add_asset(
    body: AddAssetIn,
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    try:
        asset = await catalog.add_from_search(session, market, body.source, body.symbol)
    except catalog.UnsupportedInstrument as exc:
        raise error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "unsupported_instrument", str(exc)
        ) from None
    except NotListed:
        raise error(
            status.HTTP_404_NOT_FOUND, "not_listed", "No source can price this instrument."
        ) from None
    except (DataUnavailable, SourceError):
        raise error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "data_unavailable",
            "The price source did not answer. Try again in a moment.",
        ) from None
    return AssetOut.of(asset)


@router.get("/{asset_id}", response_model=AssetOut)
async def asset_detail(asset: Asset = Depends(get_asset)):
    return AssetOut.of(asset)
