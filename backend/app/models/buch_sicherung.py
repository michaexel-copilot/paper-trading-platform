"""Nachweis je Lauf der täglichen externen Buchsicherung (B-16, HED-85, Plan Revision 4).

Eine Zeile je Versuch, erfolgreich oder nicht -- Nur-Anfügen, wie ``StopAbgleichLauf``
(B-12) und ``BoardNotification`` (HED-43). Ein Fehlschlag setzt hier bewusst **kein**
Sperrkennzeichen -- B-16 ist eine Aufbewahrungspflicht, keine Handelsvoraussetzung wie
B-5/B-12. Sichtbarkeit entsteht über ``ergebnis``/``fehlertext`` (Dashboard-Warnbanner)
und die CRO-Mail aus ``app.notify.notify_backup_failure``.
"""

from datetime import datetime

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base, BetriebsartMixin

BUCH_SICHERUNG_ERGEBNISSE = ("erfolg", "fehlgeschlagen")


class BuchSicherungLauf(Base, BetriebsartMixin):
    __tablename__ = "buch_sicherung_lauf"

    id: Mapped[int] = mapped_column(primary_key=True)
    begonnen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    beendet_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    ziel_remote: Mapped[str] = mapped_column(String(80))
    dateiname: Mapped[str | None] = mapped_column(String(120))
    bytes_gesichert: Mapped[int | None] = mapped_column(BigInteger)
    ergebnis: Mapped[str] = mapped_column(String(15))  # erfolg | fehlgeschlagen
    fehlertext: Mapped[str | None] = mapped_column(String(500))
    cro_alert_gesendet: Mapped[bool] = mapped_column(default=False)
