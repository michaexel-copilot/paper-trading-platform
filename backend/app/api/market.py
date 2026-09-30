import asyncio
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import error, get_asset, get_market
from app.api.schemas import BarOut, HistoryOut, MarketStatusOut, QuoteOut
from app.auth import current_user
from app.db import get_session
from app.marketdata.base import DataUnavailable
from app.marketdata.service import MarketService
from app.models import Asset

router = APIRouter(prefix="/api/market", tags=["market"], dependencies=[Depends(current_user)])

MAX_BATCH = 100


def _unavailable(exc: DataUnavailable):
    return error(status.HTTP_503_SERVICE_UNAVAILABLE, "data_unavailable", str(exc))


@router.get("/quote/{asset_id}", response_model=QuoteOut)
async def quote(asset: Asset = Depends(get_asset), market: MarketService = Depends(get_market)):
    try:
        return QuoteOut.of(asset.id, await market.view(asset))
    except DataUnavailable as exc:
        raise _unavailable(exc) from None


@router.get("/quotes", response_model=dict[int, QuoteOut | None])
async def quotes(
    ids: str = Query(description="Comma-separated asset ids"),
    session: AsyncSession = Depends(get_session),
    market: MarketService = Depends(get_market),
):
    """Quotes for several assets at once. An asset without a quote maps to null."""
    try:
        wanted = sorted({int(part) for part in ids.split(",") if part.strip()})[:MAX_BATCH]
    except ValueError:
        raise error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_ids", "ids must be integers."
        ) from None
    assets = (await session.scalars(select(Asset).where(Asset.id.in_(wanted)))).all()

    async def one(asset: Asset) -> tuple[int, QuoteOut | None]:
        try:
            return asset.id, QuoteOut.of(asset.id, await market.view(asset))
        except DataUnavailable:
            return asset.id, None

    return dict(await asyncio.gather(*(one(asset) for asset in assets)))


@router.get("/status/{asset_id}", response_model=MarketStatusOut)
async def market_status(
    asset: Asset = Depends(get_asset), market: MarketService = Depends(get_market)
):
    current = market.status(asset)
    return MarketStatusOut(asset_id=asset.id, is_open=current.is_open, next_open=current.next_open)


@router.get("/history/{asset_id}", response_model=HistoryOut)
async def history(
    resolution: Literal["1d", "1h"] = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
    asset: Asset = Depends(get_asset),
    market: MarketService = Depends(get_market),
):
    """Bars in ascending time order. A range with no bars returns an empty list."""
    for value in (start, end):
        if value is not None and value.tzinfo is None:
            raise error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "naive_datetime",
                "start and end need a timezone, for example 2026-01-01T00:00:00Z.",
            )
    try:
        source, bars = await market.history(asset, resolution, start, end)
    except DataUnavailable as exc:
        raise _unavailable(exc) from None
    return HistoryOut(
        asset_id=asset.id,
        resolution=resolution,
        source=source,
        bars=[BarOut.model_validate(bar) for bar in bars],
    )
