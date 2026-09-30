from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer

from app.marketdata.service import QuoteView
from app.models import Asset


def _plain(value: Decimal) -> str:
    """Decimal as a plain string without exponent or trailing zeros."""
    return format(value.normalize(), "f")


# Decimals cross the API as strings, so no client ever rounds them through a float.
Dec = Annotated[Decimal, PlainSerializer(_plain, return_type=str, when_used="json")]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- accounts ---------------------------------------------------------------


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class UserOut(Out):
    id: int
    email: str
    created_at: datetime


# --- catalog and market data -------------------------------------------------


class AssetOut(Out):
    id: int
    symbol: str
    name: str
    asset_class: str
    quote_currency: str
    quantity_step: Dec
    min_order_size: Dec | None
    calendar: str
    available: bool
    source: str | None
    seeded: bool
    rank: int | None

    @classmethod
    def of(cls, asset: Asset) -> "AssetOut":
        return cls(
            id=asset.id,
            symbol=asset.symbol,
            name=asset.name,
            asset_class=asset.asset_class,
            quote_currency=asset.quote_currency,
            quantity_step=asset.quantity_step,
            min_order_size=asset.min_order_size,
            calendar=asset.calendar,
            available=asset.available,
            source=asset.resolved_source,
            seeded=asset.seeded,
            rank=asset.rank,
        )


class AssetClassOut(BaseModel):
    asset_class: str
    count: int


class SearchResultOut(BaseModel):
    source: str
    symbol: str
    name: str
    instrument_type: str
    asset_class: str | None
    supported: bool
    exchange: str | None
    asset_id: int | None


class AddAssetIn(BaseModel):
    source: str
    symbol: str


class QuoteOut(BaseModel):
    asset_id: int
    last: Dec
    bid: Dec | None
    ask: Dec | None
    last_price_only: bool
    currency: str
    source: str
    observed_at: datetime
    age_seconds: float
    fresh: bool
    stale: bool
    delayed: bool
    market_open: bool
    next_open: datetime | None

    @classmethod
    def of(cls, asset_id: int, view: QuoteView) -> "QuoteOut":
        quote = view.quote
        return cls(
            asset_id=asset_id,
            last=quote.last,
            bid=quote.bid,
            ask=quote.ask,
            last_price_only=quote.last_price_only,
            currency=quote.currency,
            source=quote.source,
            observed_at=quote.observed_at,
            age_seconds=view.age_seconds,
            fresh=view.fresh,
            stale=view.stale,
            delayed=view.delayed,
            market_open=view.market_open,
            next_open=view.next_open,
        )


class MarketStatusOut(BaseModel):
    asset_id: int
    is_open: bool
    next_open: datetime | None


class BarOut(Out):
    time: datetime
    open: Dec
    high: Dec
    low: Dec
    close: Dec
    volume: Dec | None


class HistoryOut(BaseModel):
    asset_id: int
    resolution: str
    source: str | None
    bars: list[BarOut]


# --- fees ---------------------------------------------------------------------


class ExchangeRateOut(BaseModel):
    maker: Dec
    taker: Dec
    source_url: str | None = None
    note: str | None = None


class FeeProfileOut(Out):
    key: str
    name: str
    venue: str
    asset_classes: list[str]
    default_for: list[str]
    currency: str | None
    maker_rate: Dec
    taker_rate: Dec
    per_unit: Dec
    fixed: Dec
    min_fee: Dec | None
    max_fee: Dec | None
    max_fee_rate: Dec | None
    assumed_spread: Dec
    exchange_rates: dict[str, ExchangeRateOut] | None
    source_url: str | None
    source_note: str | None
    checked_on: date


class FeeSelectionIn(BaseModel):
    profile_key: str


# --- portfolios ---------------------------------------------------------------


class PortfolioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_currency: str
    starting_cash: Decimal


class PortfolioRename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PortfolioSummary(BaseModel):
    id: int
    name: str
    base_currency: str
    starting_cash: Dec
    created_at: datetime
    total_value: Dec
    total_return: Dec
    total_return_pct: Dec
    stale: bool


class PositionOut(BaseModel):
    asset: AssetOut
    quantity: Dec
    committed: Dec
    avg_cost: Dec
    price: Dec | None
    price_currency: str
    price_base: Dec | None
    market_value: Dec
    unrealized: Dec
    unrealized_pct: Dec | None
    stale: bool
    unpriced: bool
    quote_source: str | None
    quote_observed_at: datetime | None


class TrackedAssetOut(BaseModel):
    asset: AssetOut
    quote: QuoteOut | None
    position_quantity: Dec


class PortfolioDetail(PortfolioSummary):
    cash: Dec
    reserved_cash: Dec
    available_cash: Dec
    realized: Dec
    unrealized: Dec
    fees_paid: Dec
    fees_by_class: dict[str, Dec]
    oldest_quote_age_seconds: float | None
    positions: list[PositionOut]
    tracked: list[TrackedAssetOut]


class TrackAssetIn(BaseModel):
    asset_id: int


class SnapshotOut(Out):
    at: datetime
    value: Dec
    kind: str


# --- orders and trades ---------------------------------------------------------


class OrderIn(BaseModel):
    asset_id: int
    side: Literal["buy", "sell"]
    type: Literal["market", "limit", "stop"]
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str = Field(min_length=8, max_length=64)


class PreviewIn(BaseModel):
    asset_id: int
    side: Literal["buy", "sell"]
    type: Literal["market", "limit", "stop"]
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None


class PreviewOut(BaseModel):
    estimated_price: Dec
    price_currency: str
    fx_rate: Dec
    liquidity: str
    fills_now: bool
    value_base: Dec
    fee: Dec
    cash_change: Dec
    base_currency: str
    fee_profile_name: str
    fee_rate_fallback: bool
    quote_source: str | None
    quote_observed_at: datetime | None
    available_cash: Dec


class OrderOut(Out):
    id: int
    portfolio_id: int
    asset_id: int
    asset_symbol: str
    client_order_id: str
    side: str
    type: str
    quantity: Dec
    limit_price: Dec | None
    stop_price: Dec | None
    status: str
    reserved_cash: Dec
    reject_reason: str | None
    created_at: datetime
    closed_at: datetime | None
    trade_id: int | None = None


class TradeOut(Out):
    id: int
    order_id: int
    portfolio_id: int
    asset_id: int
    asset_symbol: str
    asset_class: str
    executed_at: datetime
    side: str
    quantity: Dec
    price: Dec
    quote_currency: str
    quote_source: str
    quote_observed_at: datetime
    fx_rate: Dec
    value_base: Dec
    liquidity: str
    fee: Dec
    fee_profile_name: str
    fee_terms: dict
    fee_rate_fallback: bool
    cash_change: Dec
    realized_pnl: Dec
