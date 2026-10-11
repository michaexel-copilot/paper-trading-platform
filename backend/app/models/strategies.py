from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

GATE_STATUSES = ("S1", "S2", "S3")


class Strategy(Base):
    """Minimal record: only what the proposal gate (app.trading.proposals) needs to read.

    Gate assignment is a separate process, outside this model and outside this ticket
    (HED-41 / HED-36 Entwurf Abschnitt 4).

    ``portfolio_id`` (HED-44, B-2 Stufe 1/2): das Buch, gegen das NAV, Drawdown,
    offene Positionen und Summenrisiko für Vorschläge dieser Strategie berechnet
    werden. Nullable, weil HED-41 Strategien ohne Buch kennt und das so bleiben soll
    -- eine Strategie ohne Buch kann per Festlegung keinen Vorschlag durch Stufe 1
    bringen (nicht berechenbar, fail closed), statt dass das Feld nachträglich
    Pflicht wird und bestehende Datensätze bricht.
    """

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    gate_status: Mapped[str] = mapped_column(String(2), default="S1", index=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="SET NULL")
    )
