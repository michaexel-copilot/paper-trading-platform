from datetime import date
from decimal import Decimal

from sqlalchemy import JSON, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import Money
from app.models.base import Base


class FeeProfile(Base):
    __tablename__ = "fee_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    venue: Mapped[str] = mapped_column(String(120))
    asset_classes: Mapped[list[str]] = mapped_column(JSON)
    default_for: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Currency of the absolute charges below; None when the profile has none.
    currency: Mapped[str | None] = mapped_column(String(8))
    maker_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    taker_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    per_unit: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fixed: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    min_fee: Mapped[Decimal | None] = mapped_column(Money)
    max_fee: Mapped[Decimal | None] = mapped_column(Money)
    max_fee_rate: Mapped[Decimal | None] = mapped_column(Money)
    assumed_spread: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    # Base-tier rates per pricing exchange, from each exchange's published schedule:
    # {"okx": {"maker": "0.0008", "taker": "0.0010", "source_url": "..."}}. When set,
    # a fill is charged the rates of the exchange that priced it.
    exchange_rates: Mapped[dict | None] = mapped_column(JSON)

    source_url: Mapped[str | None] = mapped_column(String(500))
    source_note: Mapped[str | None] = mapped_column(String(500))
    checked_on: Mapped[date] = mapped_column(Date)


class PortfolioFeeProfile(Base):
    __tablename__ = "portfolio_fee_profiles"
    __table_args__ = (UniqueConstraint("portfolio_id", "asset_class"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    asset_class: Mapped[str] = mapped_column(String(16))
    fee_profile_id: Mapped[int] = mapped_column(ForeignKey("fee_profiles.id"))
