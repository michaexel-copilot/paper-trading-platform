"""Startgatter: Auflage 1 (Startgatter 1-2) und Auflage 4 (Startgatter 3).

Läuft einmal beim Prozessstart, vor dem ersten Request. Jeder Fehlschlag verhindert den
Start vollständig -- kein eingeschränkter Modus, kein Nur-Lese-Modus, keine Warnung. Der
Grund wird protokolliert; die Weiterleitung an den Board-Benachrichtigungskanal ist
Sache des Betreibers der jeweiligen Installation (Logging-Weiterleitung, kein Code hier).

Die Schlüsseltrennung (Auflage 2) ist bewusst nicht hier verdrahtet: Es gibt noch keine
Gegenstelle, die ein Schlüssel schützen müsste (B-6 wartet auf das OKX-Demo-Secret).
``app.schluessel.read_exchange_credential`` führt seine eigene Prüfung bei jedem Aufruf
selbst aus, sobald ein Aufrufer -- das wird ausschließlich der Versand-Engpass sein --
tatsächlich einen Schlüssel braucht. Eine generische Installation dieser Plattform ohne
Gegenstellen-Anbindung hat sonst nichts, was dieses Gatter sinnvoll prüfen könnte.
"""

import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.betriebsart import Betriebsart, get_betriebsart
from app.models.installation import InstallationMarker

log = logging.getLogger(__name__)

ARTEFAKT_ARTEN = ("papier_artefakt", "live_artefakt")
_BETRIEBSART_PRAEFIX = {Betriebsart.PAPIER: "papier", Betriebsart.LIVE: "live"}


class StartgatterFehler(RuntimeError):
    """Die Installation startet nicht."""


def lese_artefakt_art() -> str:
    roh = os.environ.get("ARTEFAKT_ART")
    if roh not in ARTEFAKT_ARTEN:
        raise StartgatterFehler(
            f"ARTEFAKT_ART muss eines von {ARTEFAKT_ARTEN} sein, nicht {roh!r}. "
            "Start verweigert (Auflage 4, Startgatter 3)."
        )
    return roh


async def pruefe_installation_marker(session: AsyncSession) -> None:
    """Auflage 1, Startgatter 2: Datenbank-Markierung muss zur Prozess-Betriebsart
    passen; eine leere Datenbank wird mit ihr markiert und ist danach festgelegt.
    Auflage 4, Startgatter 3: Artefakt-Markierung muss zur Host-Betriebsart passen.
    """
    betriebsart = get_betriebsart()
    artefakt_art = lese_artefakt_art()
    erwartetes_praefix = _BETRIEBSART_PRAEFIX[betriebsart]
    if not artefakt_art.startswith(erwartetes_praefix):
        raise StartgatterFehler(
            f"Artefakt {artefakt_art!r} passt nicht zur Host-Betriebsart "
            f"{betriebsart.value!r}. Start verweigert (Auflage 4, Startgatter 3)."
        )

    try:
        marker = await session.scalar(select(InstallationMarker).limit(1))
    except Exception as exc:
        raise StartgatterFehler(
            "Installationsmarkierung nicht lesbar. Start verweigert, fail closed."
        ) from exc

    if marker is None:
        session.add(InstallationMarker(betriebsart=betriebsart.value, artefakt_art=artefakt_art))
        await session.commit()
        log.info(
            "Neue Datenbank mit Betriebsart %r, Artefakt %r markiert.",
            betriebsart.value,
            artefakt_art,
        )
        return

    if marker.betriebsart != betriebsart.value:
        raise StartgatterFehler(
            f"Datenbank ist mit Betriebsart {marker.betriebsart!r} markiert, Prozess "
            f"läuft als {betriebsart.value!r}. Start verweigert (Auflage 1, Startgatter 2)."
        )
    if marker.artefakt_art != artefakt_art:
        raise StartgatterFehler(
            f"Datenbank ist mit Artefakt {marker.artefakt_art!r} markiert, Prozess läuft "
            f"als {artefakt_art!r}. Start verweigert (Auflage 4, Startgatter 3)."
        )
