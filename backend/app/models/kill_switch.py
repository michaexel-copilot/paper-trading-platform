"""Unveränderliches Protokoll für den Lebenszyklus der zwei Sperrkennzeichen (B-3,
Entwurf HED-36 Fassung 1.1 Abschnitt 9.4/9.5).

Die Grundtabellen (``app.models.sperrkennzeichen``, HED-42) speichern nur den
aktuellen Zustand. Dieses Modul ergänzt, was HED-42 bewusst ausgelassen hat: jede
Setzen- oder Aufheben-Aktion wird hier als eigene, nie überschriebene Zeile
mitgeschrieben -- Begründung, Entscheider, Zeitstempel. Nur anfügen, wie bei
``ProposalEvent``.
"""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base

KENNZEICHEN_TYPEN = ("firmenweit", "strategie")
AKTIONEN = ("gesetzt", "aufgehoben")

# Auslöser, die eine Aktion dokumentieren. "board" / "cro" sind manuelle Aktionen nach
# Abschnitt 9.1/9.4. "stufe1"/"stufe2" sind die Kill-Switch-Stufen selbst (Abschnitt
# 9.1-Tabelle). Die übrigen fünf sind die automatischen Setzen-Auslöser aus Abschnitt
# 9.5 -- für sie existiert strukturell keine Aufheben-Variante (siehe app.kill_switch).
AUSLOESER = (
    "board",
    "cro",
    "stufe1",
    "stufe2",
    "auflagen_selbsttest_fehlgeschlagen",
    "betriebsart_abweichung",
    "order_ohne_versandengpass_eintrag",
    "protokoll_nicht_schreibbar",
    "limitliste_nicht_lesbar",
)


class SperrkennzeichenProtokoll(Base):
    __tablename__ = "sperrkennzeichen_protokoll"

    id: Mapped[int] = mapped_column(primary_key=True)
    kennzeichen_typ: Mapped[str] = mapped_column(String(10))  # firmenweit | strategie
    # Nur bei kennzeichen_typ == "strategie" gesetzt (Entwurf 9.1).
    strategie_schluessel: Mapped[str | None] = mapped_column(String(64), index=True)
    aktion: Mapped[str] = mapped_column(String(10))  # gesetzt | aufgehoben
    ausloeser: Mapped[str] = mapped_column(String(50))
    # Namentliche Entscheider-Kennung (Entwurf 9.4). entscheider_2 nur bei Aufhebung
    # des firmenweiten Kennzeichens (Zwei-Personen-Regel, A19, CRO-freigegeben).
    entscheider_1: Mapped[str | None] = mapped_column(String(100))
    entscheider_2: Mapped[str | None] = mapped_column(String(100))
    begruendung: Mapped[str] = mapped_column(String(1000))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
