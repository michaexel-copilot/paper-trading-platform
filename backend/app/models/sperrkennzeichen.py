"""Minimale Speicherung der zwei Sperrkennzeichen (B-3, Entwurf Abschnitt 9.1).

Dieses Modul speichert nur den Zustand ('gesetzt' oder 'offen') fail-closed lesbar.
Der volle Lebenszyklus -- Zwei-Personen-Regel zur Aufhebung des firmenweiten
Kennzeichens, automatisches Setzen nach Abschnitt 9.5, Benachrichtigung, Protokoll der
Begründung -- ist Gegenstand des Kill-Switch-Zuschnitt-Folgeissues (HED-36-Kind) und
bewusst nicht Teil dieser Tabelle. Was hier liegt, ist die fail-closed-Grundlage, auf der
der Versand-Engpass (Auflage 3) aufsetzen muss, um überhaupt testbar zu sein.
"""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.dbtypes import UtcDateTime, utcnow
from app.models.base import Base


class FirmenweitesSperrkennzeichen(Base):
    """Genau eine Zeile. gesetzt=True sperrt Vorschlagserstellung/Stop-Nachführung für alle."""

    __tablename__ = "firmenweites_sperrkennzeichen"

    id: Mapped[int] = mapped_column(primary_key=True)
    gesetzt: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class StrategieSperrkennzeichen(Base):
    """Eine Zeile pro Strategie, die je ein eigenes Sperrkennzeichen erhalten hat.

    Fehlt die Zeile für eine Strategie, gilt ihr Kennzeichen als 'offen' (Entwurf
    Abschnitt 9.4, Lückenfall) -- nicht als Lesefehler. Das ist der Unterschied zu einem
    tatsächlichen Lesefehler (DB nicht erreichbar), der fail-closed als 'gesetzt' gilt.
    """

    __tablename__ = "strategie_sperrkennzeichen"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategie_schluessel: Mapped[str] = mapped_column(String(64), unique=True)
    gesetzt: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)
