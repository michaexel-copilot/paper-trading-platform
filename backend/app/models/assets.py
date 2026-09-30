from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.dbtypes import Money, UtcDateTime
from app.models.base import Base


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_class: Mapped[str] = mapped_column(String(16), index=True)
    quote_currency: Mapped[str] = mapped_column(String(8))
    quantity_step: Mapped[Decimal] = mapped_column(Money)
    min_order_size: Mapped[Decimal | None] = mapped_column(Money)
    calendar: Mapped[str] = mapped_column(String(64))
    seeded: Mapped[bool] = mapped_column(default=False)
    rank: Mapped[int | None] = mapped_column()

    available: Mapped[bool] = mapped_column(default=True)
    resolved_source: Mapped[str | None] = mapped_column(String(32))
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Most recently recorded price, used to value a position when no quote is obtainable.
    last_price: Mapped[Decimal | None] = mapped_column(Money)
    last_price_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    source_symbols: Mapped[list["AssetSourceSymbol"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", lazy="selectin"
    )


class AssetSourceSymbol(Base):
    __tablename__ = "asset_source_symbols"
    __table_args__ = (UniqueConstraint("asset_id", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))

    asset: Mapped[Asset] = relationship(back_populates="source_symbols")
