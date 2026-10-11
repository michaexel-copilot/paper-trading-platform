"""Zählbarer Nachweis jeder Versand-Engpass-Entscheidung (Auflage 3, Abschnitt 3.3).

Jede Zeile ist ein abgeschlossener Entscheid des Versand-Engpasses -- angenommen oder
abgelehnt, mit Begründung bei Ablehnung. Reihen werden eingefügt und nie verändert. Der
wiederkehrende Abgleich gegen die Ordernachweise der Gegenstelle (Testpfad 10) liest
diese Tabelle, baut sie aber nicht selbst.
"""

from datetime import datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base, BetriebsartMixin


class VersandEngpassEintrag(Base, BetriebsartMixin):
    __tablename__ = "versand_engpass_eintrag"

    id: Mapped[int] = mapped_column(primary_key=True)
    gattung: Mapped[str] = mapped_column(String(20), index=True)
    strategie_schluessel: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(20))
    angenommen: Mapped[bool] = mapped_column()
    ablehnungsgrund: Mapped[str | None] = mapped_column(String(200))
    order_referenz: Mapped[dict] = mapped_column(JSON)
    gegenstellen_order_id: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
