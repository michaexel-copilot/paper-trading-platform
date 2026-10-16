"""Eigenes Stop-Buch (B-12, HED-84, Entwurf Abschnitt 10.2, Festlegung F-15).

Bewusst **nicht** aus ``VersandEngpassEintrag`` abgeleitet -- diese Tabelle bleibt eine
Nur-Anfügen-Protokolltabelle ohne Lebenszyklus-Feld (Begründung siehe Entwurf Abschnitt
10.2). ``StopOrderBuch`` ist stattdessen die Arbeitstabelle des täglichen Abgleichs
(``app.trading.stop_orderbuch_abgleich``), ``StopAbgleichLauf`` ihr Nachweis je
Durchlauf und zugleich der Totmannschalter (Entwurf Abschnitt 10.6).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import Money, UtcDateTime, utcnow
from app.models.base import Base, BetriebsartMixin

STOP_ORDER_ZUSTAENDE = ("offen", "nachfuehrung_laeuft", "ersetzt", "storniert", "ausgeloest")
STOP_ABGLEICH_ERGEBNISSE = ("bestanden", "abweichung", "fehlgeschlagen")


class StopOrderBuch(Base, BetriebsartMixin):
    """Eine Zeile je börsenseitiger Stop-Order (Entwurf 10.2, Tabelle)."""

    __tablename__ = "stop_order_buch"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategie_schluessel: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(5))  # buy | sell

    menge_soll: Mapped[Decimal] = mapped_column(Money)
    # Erst nach erfolgreicher Platzierung gesetzt -- was OKX nach Rundung auf die
    # Instrumentengenauigkeit tatsächlich führt (Entwurf 10.2, "einmalig gelesen").
    menge_bestaetigt: Mapped[Decimal | None] = mapped_column(Money)
    trigger_soll: Mapped[Decimal] = mapped_column(Money)
    trigger_bestaetigt: Mapped[Decimal | None] = mapped_column(Money)

    gegenstellen_algo_id: Mapped[str | None] = mapped_column(String(100), index=True)
    anlass_proposal_id: Mapped[int | None] = mapped_column()
    versand_eintrag_id: Mapped[int | None] = mapped_column()

    zustand: Mapped[str] = mapped_column(String(20), default="offen", index=True)
    ersetzt_durch_id: Mapped[int | None] = mapped_column()

    # HED-147 (Entwurf Abschnitt 4.3, Festlegung N-7): bereits gebuchte Fill-Menge
    # dieses Stops -- ohne dieses Delta-Feld würde jeder Durchlauf der
    # Exit-Fill-Erkennung eine bereits gebuchte Teilfüllung erneut buchen.
    # Nullable wie alle neuen Spalten aus diesem Issue (Festlegung N-6); Default 0
    # über Migration (server_default) und ORM-Default gleichermaßen.
    gefuellte_menge_gebucht: Mapped[Decimal | None] = mapped_column(Money, default=Decimal(0))

    erstellt_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    beendet_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class StopAbgleichLauf(Base, BetriebsartMixin):
    """Nachweis je Durchlauf des täglichen Abgleichs, nur anfügen (Entwurf 10.2/10.6).

    Gleichzeitig der Totmannschalter: das Alter der jüngsten Zeile mit
    ``ergebnis == "bestanden"`` ist die Grundlage für ``stop_orderbuch_abgleich_fehlgeschlagen``
    (Entwurf 10.6, Punkt 2).
    """

    __tablename__ = "stop_abgleich_lauf"

    id: Mapped[int] = mapped_column(primary_key=True)
    begonnen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    beendet_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    anzahl_eigene_offene: Mapped[int] = mapped_column(default=0)
    anzahl_gegenstelle_offene: Mapped[int] = mapped_column(default=0)
    anzahl_fehlend: Mapped[int] = mapped_column(default=0)
    anzahl_waisen: Mapped[int] = mapped_column(default=0)
    anzahl_mengenabweichung: Mapped[int] = mapped_column(default=0)
    anzahl_triggerabweichung: Mapped[int] = mapped_column(default=0)
    ergebnis: Mapped[str] = mapped_column(String(15))  # bestanden | abweichung | fehlgeschlagen
    fehlertext: Mapped[str | None] = mapped_column(String(500))
