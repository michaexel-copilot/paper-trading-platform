from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import Money, UtcDateTime, utcnow
from app.models.base import Base

ORDER_STATUSES = ("open", "filled", "cancelled", "rejected")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("portfolio_id", "client_order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    client_order_id: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    type: Mapped[str] = mapped_column(String(8))  # market | limit | stop
    quantity: Mapped[Decimal] = mapped_column(Money)
    limit_price: Mapped[Decimal | None] = mapped_column(Money)
    stop_price: Mapped[Decimal | None] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)
    # Cash held back from available cash while a buy order is open, in base currency.
    reserved_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    reject_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Trade(Base):
    """An immutable record of a fill. Rows are inserted and never updated."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal] = mapped_column(Money)
    quote_currency: Mapped[str] = mapped_column(String(8))
    quote_source: Mapped[str] = mapped_column(String(32))
    quote_observed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    fx_rate: Mapped[Decimal] = mapped_column(Money)
    value_base: Mapped[Decimal] = mapped_column(Money)
    liquidity: Mapped[str] = mapped_column(String(5))  # maker | taker
    fee: Mapped[Decimal] = mapped_column(Money)
    fee_profile_name: Mapped[str] = mapped_column(String(120))
    fee_terms: Mapped[dict] = mapped_column(JSON)
    fee_rate_fallback: Mapped[bool] = mapped_column(default=False)
    cash_change: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
