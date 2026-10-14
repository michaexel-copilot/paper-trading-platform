from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import Money, UtcDateTime, utcnow
from app.models.base import Base


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    base_currency: Mapped[str] = mapped_column(String(8))
    starting_cash: Mapped[Decimal] = mapped_column(Money)
    cash: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fees_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    # Barbestand außerhalb der Börse (HED-8 Abschnitt 4.1, B-11/HED-83). Im
    # Papierbetrieb immer 0 -- es gibt keine echte externe Bankreserve, nur den
    # simulierten Börsenbestand in ``cash``. Das Feld existiert für den Live-Pfad
    # (Gate C); diese PR liefert keine API, um es zu setzen (siehe PR-Beschreibung).
    external_reserve_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class PortfolioAsset(Base):
    """An asset tracked in a portfolio, whether or not a position is held."""

    __tablename__ = "portfolio_assets"
    __table_args__ = (UniqueConstraint("portfolio_id", "asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    added_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("portfolio_id", "asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    quantity: Mapped[Decimal] = mapped_column(Money)
    # Average cost per unit in the portfolio's base currency, fees excluded.
    avg_cost: Mapped[Decimal] = mapped_column(Money)


class ValueSnapshot(Base):
    __tablename__ = "value_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    value: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(16))  # created | fill | daily
    # Je gehaltenem Asset Menge, Kurs in Quote-Währung und FX-Kurs zur Basiswährung,
    # zum Zeitpunkt dieses Schnappschusses (B-11/HED-83, app.trading.valuation.
    # breakdown_json) -- Basis für die Tages-P&L-Aufteilung in Handelsergebnis und
    # Währungseffekt. Nur für ``kind="daily"`` befüllt; ``None`` bei älteren Zeilen
    # (vor B-11) und bei "created"/"fill".
    breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
