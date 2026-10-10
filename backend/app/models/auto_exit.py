"""Protokoll für Kategorie (iv) des Freigabe-Protokolls: automatischer

strategiegetriebener Ausstieg (HED-36 Entwurf Fassung 1.1, Abschnitt 8.2/8.4,
Tabelle 8.2; Umsetzung HED-46).

Kein Proposal-Zustand (die Entwurf-Zustandstabelle 4.2 kennt diesen Pfad nicht) --
ein automatischer Ausstieg bekommt einen eigenen, von `Proposal`/`ProposalEvent`
unabhängigen Datensatz. Dieses Modul legt nur das Protokollformat fest. Die
Prüfkette und die Reversal-Aufspaltung, die eine Zeile hier erzeugen, sind HED-48
(Entwurf Abschnitt 10), außerhalb dieses Umfangs.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import Money, UtcDateTime, utcnow
from app.models.base import Base

# Kanonischer Bezeichner der Kategorie, Tabelle 8.2 -- hier nur als Referenz für
# Protokollansichten, die alle fünf Listen aus 8.6 zusammenstellen; kein Spaltenwert,
# weil jede Zeile dieser Tabelle per Definition dieser Kategorie angehört.
LOG_CATEGORY = "strategie_ausstieg"

ORDER_DIRECTIONS = ("kauf", "verkauf")
POSITION_DIRECTIONS_CLOSED = ("long", "short")
RESULTS = (
    "versandt",
    "gefuellt",
    "teilgefuellt",
    "abgelehnt_groesse",
    "abgelehnt_richtung",
    "eskaliert_kursfrische",
    "von_gegenstelle_abgelehnt",
)


class AutoExitLogEntry(Base):
    """Ein unveränderlicher Protokolleintrag je automatischem Ausstiegsvorgang

    (Entwurf 8.4). Zeilen werden eingefügt und nie aktualisiert; eine Korrektur ist
    ein neuer Datensatz mit `correction_of_id` (Entwurf 8.5).
    """

    __tablename__ = "auto_exit_log_entries"

    id: Mapped[int] = mapped_column(primary_key=True)

    mode: Mapped[str] = mapped_column(String(4))  # paper | live
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)

    trigger_signal_id: Mapped[str] = mapped_column(String(64), index=True)
    trigger_signal_kind: Mapped[str] = mapped_column(String(50))
    trigger_at: Mapped[datetime] = mapped_column(UtcDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    filled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    instrument: Mapped[str] = mapped_column(String(64))
    exit_size: Mapped[Decimal] = mapped_column(Money)
    position_before: Mapped[Decimal] = mapped_column(Money)
    position_after: Mapped[Decimal | None] = mapped_column(Money)
    order_direction: Mapped[str] = mapped_column(String(7))  # kauf | verkauf
    position_direction_closed: Mapped[str] = mapped_column(String(5))  # long | short

    price_age_seconds: Mapped[int] = mapped_column()
    price_source_timestamp: Mapped[datetime] = mapped_column(UtcDateTime)

    # Festlegung A4.1 (Entwurf 10.2): Größe darf auf die offene Position begrenzt werden.
    size_was_capped: Mapped[bool] = mapped_column(default=False)
    size_capped_to: Mapped[Decimal | None] = mapped_column(Money)

    # Entwurf 10.4: Reversal-Signale erzeugen getrennt einen Gegenposition-Vorschlag.
    is_reversal: Mapped[bool] = mapped_column(default=False)
    reversal_counter_proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )

    # Zustand beider Sperrkennzeichen zum Versandzeitpunkt (gelesen, nicht blockierend
    # für diesen Pfad -- Tabelle 9.3). Nur gelesen/protokolliert, Prüfung ist HED-47.
    firmwide_lock_state: Mapped[str] = mapped_column(String(30))
    strategy_lock_state: Mapped[str] = mapped_column(String(30))

    result: Mapped[str] = mapped_column(String(30))

    # Entwurf 10.3: Eskalation an CRO + Board bei zu altem Kurs.
    escalation_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    escalation_recipients: Mapped[list | None] = mapped_column(JSON)
    escalation_deadline: Mapped[datetime | None] = mapped_column(UtcDateTime)
    escalation_result: Mapped[str | None] = mapped_column(String(200))

    correction_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_exit_log_entries.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
