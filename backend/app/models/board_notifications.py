"""Nachweis des Board-Benachrichtigungsversands je Vorschlag (HED-43, Entwurf 5.3).

Genau eine Zeile pro Vorschlag, der Z3 "wartet_auf_board" erreicht hat. ``status``
startet bei ``pending``, wird nach einem erfolgreichen Versand ``sent`` und erst nach
dem zweiten erfolglosen Versuch (ein Erstversuch, eine Wiederholung) dauerhaft
``failed`` -- dieser Zustand löst das Warnbanner im Dashboard und die Einzelmeldung an
den CRO aus (Entwurf 5.3, Fehlerverhalten).
"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base, BetriebsartMixin

NOTIFICATION_STATES = ("pending", "sent", "failed")


class BoardNotification(Base, BetriebsartMixin):
    __tablename__ = "board_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(10), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    cro_alert_sent: Mapped[bool] = mapped_column(default=False)
