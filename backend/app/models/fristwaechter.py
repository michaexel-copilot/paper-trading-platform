"""Pfad 7 -- Fristwächter-Eskalation (B-15, HED-185, CRO-Entscheidung HED-191
Abschnitt 2, Option C: nur Eskalation/Protokoll, keine automatische Order).

Nur anfügen, wie ``SperrkennzeichenProtokoll``. Dient ausschließlich als Dedup-Marke:
höchstens eine Eskalationszeile je (kennzeichen_typ, strategie_schluessel,
ausloese_zeitpunkt)-Tripel -- siehe ``app.trading.fristwaechter.ausloese_zeitpunkt``
für die Herkunft von ``ausloese_zeitpunkt``. Ein neuer Auslösezeitpunkt (nach einer
zwischenzeitlichen Aufhebung) erlaubt eine neue Eskalation für denselben Schlüssel.
"""

from datetime import datetime

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base, BetriebsartMixin

FRIST_TYPEN = ("stufe1_schliessung", "stufe2_exposure_rueckfuehrung")


class FristEskalation(Base, BetriebsartMixin):
    __tablename__ = "frist_eskalation"
    __table_args__ = (
        UniqueConstraint(
            "kennzeichen_typ", "strategie_schluessel", "ausloese_zeitpunkt",
            name="uq_frist_eskalation_trigger",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kennzeichen_typ: Mapped[str] = mapped_column(String(10))  # firmenweit | strategie
    strategie_schluessel: Mapped[str | None] = mapped_column(String(64), index=True)
    frist_typ: Mapped[str] = mapped_column(String(30))
    # Frühester "gesetzt"-Zeitpunkt der laufenden, ununterbrochenen Sperre (Dedup-Schlüssel).
    ausloese_zeitpunkt: Mapped[datetime] = mapped_column(UtcDateTime)
    befund: Mapped[str] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
