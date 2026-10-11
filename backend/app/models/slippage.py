"""Zweiphasige Slippage-Messung (Entwurf HED-36 Fassung 1.1 Abschnitt 11, siehe HED-49).

Eine Zeile pro Vorschlag, geschrieben genau einmal, sobald der Vorschlag einen
Füllungs-Zeitpunkt hat (gefüllt oder teilgefüllt beendet). Zeilen werden eingefügt und
nie verändert -- derselbe Grundsatz wie bei ``ProposalEvent``/``VersandEngpassEintrag``:
eine spätere Fehlbedienung am Proposal-Datensatz darf eine bereits berichtete Messung
nicht rückwirkend verändern.

Phase 1 (Signal -> Freigabe) und Phase 2 (Freigabe -> Füllung) werden getrennt
gespeichert (Festlegung 11.2). Es gibt absichtlich keine Spalte für eine
zusammengefasste Gesamt-Slippage -- eine solche Kennzahl ist nach Abschnitt 11.2 nicht
zulässig, weil sie den unfairen Vergleich der Board-Reaktionszeit gegen Paper durch die
Hintertür wieder einführen würde.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import Money, UtcDateTime, utcnow
from app.models.base import Base, BetriebsartMixin


class SlippageMessungEintrag(Base, BetriebsartMixin):
    __tablename__ = "slippage_messung_eintrag"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), unique=True, index=True
    )

    # Phase 1 (Entwurf 11.1/11.5) -- niemals gegen den Paper-Wert verglichen.
    dauer_signal_bis_freigabe_sekunden: Mapped[int] = mapped_column()
    kursveraenderung_signal_bis_freigabe: Mapped[Decimal | None] = mapped_column(Money)

    # Phase 2 (Entwurf 11.1/11.5) -- Hauptwerte im Paper-Vergleich sind
    # dauer_freigabe_bis_fuellung_sekunden und preisabweichung_risikoeinheiten.
    dauer_freigabe_bis_versand_sekunden: Mapped[int] = mapped_column()
    dauer_versand_bis_fuellung_sekunden: Mapped[int] = mapped_column()
    dauer_freigabe_bis_fuellung_sekunden: Mapped[int] = mapped_column()
    fuellpreis: Mapped[Decimal] = mapped_column(Money)
    referenzkurs: Mapped[Decimal] = mapped_column(Money)
    preisabweichung_absolut: Mapped[Decimal] = mapped_column(Money)
    preisabweichung_prozentpunkte: Mapped[Decimal] = mapped_column(Money)
    preisabweichung_risikoeinheiten: Mapped[Decimal] = mapped_column(Money)

    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
