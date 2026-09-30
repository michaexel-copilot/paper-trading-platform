"""The asset catalog: seeding, availability checks, search and adding assets."""

import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ASSET_CLASSES
from app.db import SessionFactory
from app.dbtypes import utcnow
from app.marketdata.base import DataUnavailable, Instrument, NotListed, Quote
from app.marketdata.calendars import (
    ALWAYS_OPEN,
    CALENDAR_BY_YAHOO_EXCHANGE,
    FOREX,
    is_known_calendar,
)
from app.marketdata.router import AssetRef
from app.marketdata.service import MarketService
from app.models import Asset, AssetSourceSymbol

log = logging.getLogger(__name__)

SEED_FILE = Path(__file__).parent / "seed" / "assets.yaml"
STABLECOINS = {"USDT", "USDC", "USDS", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "BUSD"}

DEFAULT_STEP = {
    "crypto": Decimal("0.00000001"),
    "stocks": Decimal(1),
    "etfs": Decimal(1),
    "commodities": Decimal("0.01"),
    "forex": Decimal("0.01"),
}
PRICE_RECORD_INTERVAL_SECONDS = 60.0


class UnsupportedInstrument(Exception):
    """The instrument fits none of the platform's asset classes."""


def load_seed(path: Path = SEED_FILE) -> list[dict]:
    """Seed entries with class defaults applied, ranked in file order."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = []
    for asset_class, block in document["classes"].items():
        for rank, item in enumerate(block["assets"], start=1):
            entries.append(
                {**block.get("defaults", {}), **item, "asset_class": asset_class, "rank": rank}
            )
    return entries


async def seed_assets(session: AsyncSession, entries: list[dict] | None = None) -> None:
    """Create missing seeded assets. Existing assets keep their identity."""
    entries = load_seed() if entries is None else entries
    existing = {asset.symbol: asset for asset in (await session.scalars(select(Asset))).all()}
    for entry in entries:
        asset = existing.get(entry["symbol"])
        if asset is None:
            asset = Asset(
                symbol=entry["symbol"],
                asset_class=entry["asset_class"],
                quote_currency=entry["quote_currency"],
                quantity_step=Decimal(str(entry["quantity_step"])),
                min_order_size=(
                    Decimal(str(entry["min_order_size"])) if entry.get("min_order_size") else None
                ),
                calendar=entry["calendar"],
                source_symbols=[],
            )
            session.add(asset)
        asset.name = entry["name"]
        asset.rank = entry["rank"]
        asset.seeded = True
        known = {link.source for link in asset.source_symbols}
        for source, symbol in entry["symbols"].items():
            if source not in known:
                asset.source_symbols.append(AssetSourceSymbol(source=source, symbol=symbol))
    await session.commit()


def calendar_for(instrument: Instrument) -> str | None:
    if instrument.instrument_type == "crypto":
        return ALWAYS_OPEN
    if instrument.instrument_type == "currency":
        return FOREX
    code = CALENDAR_BY_YAHOO_EXCHANGE.get(instrument.exchange or "")
    return code if code and is_known_calendar(code) else None


def _apply_resolution(asset: Asset, resolved: tuple[str, Instrument] | None) -> None:
    asset.resolved_at = utcnow()
    if resolved is None:
        asset.available = False
        asset.resolved_source = None
        return
    source, instrument = resolved
    asset.available = True
    asset.resolved_source = source
    if instrument.quote_currency:
        asset.quote_currency = instrument.quote_currency
    if instrument.amount_step:
        asset.quantity_step = instrument.amount_step
    if asset.asset_class == "crypto":
        asset.min_order_size = instrument.min_amount
    calendar = calendar_for(instrument)
    if calendar:
        asset.calendar = calendar


async def check_asset(session: AsyncSession, market: MarketService, asset: Asset) -> None:
    """Find the source that prices the asset and take its metadata from there.

    An asset no source lists becomes unavailable. When a source fails, nothing
    is changed, because the answer is unknown.
    """
    try:
        resolved = await market.router.resolve(market.ref(asset))
    except DataUnavailable:
        log.warning("could not check %s: a source failed", asset.symbol)
        return
    _apply_resolution(asset, resolved)
    await session.commit()


async def check_all_assets(session_factory: SessionFactory, market: MarketService) -> None:
    async with session_factory() as session:
        asset_ids = list(await session.scalars(select(Asset.id)))

    async def one(asset_id: int) -> None:
        async with session_factory() as session:
            asset = await session.get(Asset, asset_id)
            if asset is not None:
                await check_asset(session, market, asset)

    # Sources limit their own concurrency; a small fan-out keeps the check quick.
    gate = asyncio.Semaphore(4)

    async def gated(asset_id: int) -> None:
        async with gate:
            try:
                await one(asset_id)
            except Exception:
                log.exception("availability check of asset %s failed", asset_id)

    await asyncio.gather(*(gated(asset_id) for asset_id in asset_ids))


def make_price_recorder(session_factory: SessionFactory):
    """Keep each asset's most recent price and source in the database, throttled."""
    last_written: dict[int, float] = {}

    async def record(asset_id: int, quote: Quote) -> None:
        now = time.monotonic()
        if now - last_written.get(asset_id, float("-inf")) < PRICE_RECORD_INTERVAL_SECONDS:
            return
        last_written[asset_id] = now
        async with session_factory() as session:
            await session.execute(
                update(Asset)
                .where(Asset.id == asset_id)
                .values(
                    last_price=quote.last,
                    last_price_at=quote.observed_at,
                    resolved_source=quote.source,
                )
            )
            await session.commit()

    return record


def _search_sources(market: MarketService) -> list:
    names: list[str] = []
    for asset_class in ASSET_CLASSES:
        for name in market.router.chains.get(asset_class, []):
            if name in market.router.sources and name not in names:
                names.append(name)
    return [market.router.sources[name] for name in names]


async def search_instruments(
    session: AsyncSession, market: MarketService, query: str
) -> list[dict]:
    """Instruments matching the query on any source, marked with catalog membership."""

    async def from_source(source) -> list[tuple[str, Instrument]]:
        try:
            return [(source.name, found) for found in await source.search(query)]
        except Exception as exc:
            log.warning("search on %s failed: %r", source.name, exc)
            return []

    batches = await asyncio.gather(*(from_source(s) for s in _search_sources(market)))
    links = {
        (link.source, link.symbol): link.asset_id
        for link in (await session.scalars(select(AssetSourceSymbol))).all()
    }
    results, seen = [], set()
    for source_name, instrument in (hit for batch in batches for hit in batch):
        asset_class = instrument.asset_class
        # Several exchanges list the same coin; show it once.
        if asset_class == "crypto":
            key = ("crypto", _crypto_base(instrument))
            if key[1] in STABLECOINS:
                continue
        else:
            key = (source_name, instrument.symbol)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "source": source_name,
                "symbol": instrument.symbol,
                "name": instrument.name,
                "instrument_type": instrument.instrument_type,
                "asset_class": asset_class,
                "supported": asset_class is not None,
                "exchange": instrument.exchange,
                "asset_id": links.get((source_name, instrument.symbol)),
            }
        )
    return results


def _crypto_base(instrument: Instrument) -> str:
    if instrument.base:
        return instrument.base.upper()
    return instrument.symbol.replace("/", "-").split("-")[0].upper()


async def _crypto_symbols(
    market: MarketService, base: str, known: dict[str, str]
) -> dict[str, str]:
    """The coin's symbol on every source of the crypto chain that lists it."""
    symbols = dict(known)
    for name in market.router.chains.get("crypto", []):
        source = market.router.sources.get(name)
        if source is None or name in symbols:
            continue
        candidates = [f"{base}-USD"] if name == "yahoo" else [f"{base}/USDT", f"{base}/USD"]
        for candidate in candidates:
            try:
                await source.instrument(candidate)
            except Exception:
                continue
            symbols[name] = candidate
            break
    return symbols


async def add_from_search(
    session: AsyncSession, market: MarketService, source_name: str, symbol: str
) -> Asset:
    """Add an instrument found by search to the catalog, or return the existing asset."""
    source = market.router.sources.get(source_name)
    if source is None:
        raise NotListed(f"unknown source {source_name}")

    link = await session.scalar(
        select(AssetSourceSymbol).where(
            AssetSourceSymbol.source == source_name, AssetSourceSymbol.symbol == symbol
        )
    )
    if link is not None:
        return await session.get(Asset, link.asset_id)

    instrument = await source.instrument(symbol)
    asset_class = instrument.asset_class
    if asset_class is None:
        raise UnsupportedInstrument(
            f"Instrument type '{instrument.instrument_type}' is not supported. "
            "Supported: crypto, stocks, ETFs, commodity futures and currency pairs."
        )

    canonical = _crypto_base(instrument) if asset_class == "crypto" else symbol.upper()
    existing = await session.scalar(select(Asset).where(Asset.symbol == canonical))
    if existing is not None:
        return existing

    calendar = calendar_for(instrument)
    if calendar is None and hasattr(source, "trading_hours"):
        hours = await source.trading_hours(symbol)
        if hours:
            calendar = "WD|{}|{}|{}".format(*hours)
    if calendar is None:
        raise UnsupportedInstrument(
            f"The trading hours of exchange '{instrument.exchange}' are not known."
        )

    symbols = {source_name: symbol}
    if asset_class == "crypto":
        symbols = await _crypto_symbols(market, canonical, symbols)

    # Resolve before touching the database: no write may be pending while a
    # source is being asked over the network.
    ref = AssetRef(key=canonical, asset_class=asset_class, symbols=symbols)
    resolved = await market.router.resolve(ref)
    if resolved is None:
        raise NotListed(f"no source can price {symbol}")

    asset = Asset(
        symbol=canonical,
        name=instrument.name,
        asset_class=asset_class,
        quote_currency=instrument.quote_currency or "USD",
        quantity_step=instrument.amount_step or DEFAULT_STEP[asset_class],
        min_order_size=instrument.min_amount,
        calendar=calendar,
        seeded=False,
        source_symbols=[AssetSourceSymbol(source=s, symbol=sym) for s, sym in symbols.items()],
    )
    _apply_resolution(asset, resolved)
    # The calendar chosen above also covers exchanges without a calendar of their own.
    asset.calendar = calendar_for(resolved[1]) or calendar
    session.add(asset)
    await session.commit()
    return asset
