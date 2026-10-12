"""Protokoll für Signale, die die Doppelprüfung der Sperrkennzeichen vor Z0 abfängt
(Entwurf HED-36 Fassung 1.1 Abschnitt 9.2 Festlegung 3, erster Prüfpunkt).

Kein Proposal-Zustand: das Signal erzeugt in diesem Fall nie einen ``Proposal``-
Datensatz, also auch keinen ``ProposalEvent``. Dieses Modul ist das einzige
Protokoll für diesen Fall -- ohne es wäre eine unterdrückte Vorschlagserstellung
spurlos (Testpfade 1 und 4, Entwurf 9.6).
"""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base

GRUENDE = ("firmenweites_kennzeichen_gesetzt", "strategie_kennzeichen_gesetzt")


class VorschlagUnterdrueckt(Base):
    __tablename__ = "vorschlag_unterdrueckt"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(index=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    grund: Mapped[str] = mapped_column(String(50))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
