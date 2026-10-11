"""Protokoll jedes Aktionsversuchs auf einen Vorschlag im Z3-Dashboard (HED-43,
Entwurf 5.2).

Eine Zeile pro Versuch von Freigeben/Ändern/Verwerfen -- erfolgreich oder abgelehnt.
Abgelehnte Versuche (falscher Zustand, Versionskonflikt durch eine gleichzeitige
zweite Aktion) werden hier genauso festgehalten wie erfolgreiche, damit ein Rennen
zwischen zwei Board-Mitgliedern nachvollziehbar bleibt (Plan HED-43 Abschnitt 2).
"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base, BetriebsartMixin

ACTIONS = ("approve", "change", "discard")
OUTCOMES = ("applied", "rejected_state", "rejected_conflict")


class ProposalActionAttempt(Base, BetriebsartMixin):
    __tablename__ = "proposal_action_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(10))
    outcome: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str | None] = mapped_column(String(500))
    attempted_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
