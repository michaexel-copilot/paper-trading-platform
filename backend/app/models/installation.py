"""Installationsweite Markierungen (Auflage 1 Startgatter 2, Auflage 4 Startgatter 3).

Diese Tabelle trägt genau eine Zeile. Sie bindet eine Datenbank unveränderlich an die
Betriebsart und Bauart, mit der sie zuerst beschrieben wurde. Eine leere, neue Datenbank
wird beim ersten Start mit der Prozess-Betriebsart markiert; danach ist das Feld
Festlegung, keine Konfiguration.
"""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base


class InstallationMarker(Base):
    __tablename__ = "installation_marker"

    id: Mapped[int] = mapped_column(primary_key=True)
    betriebsart: Mapped[str] = mapped_column(String(5))
    artefakt_art: Mapped[str] = mapped_column(String(14))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
