"""Startgatter: Auflage 1 (Startgatter 1-2) und Auflage 4 (Startgatter 3).

Läuft einmal beim Prozessstart, vor dem ersten Request. Jeder Fehlschlag verhindert den
Start vollständig -- kein eingeschränkter Modus, kein Nur-Lese-Modus, keine Warnung. Der
Grund wird protokolliert; die Weiterleitung an den Board-Benachrichtigungskanal ist
Sache des Betreibers der jeweiligen Installation (Logging-Weiterleitung, kein Code hier).

Die Schlüsseltrennung (Auflage 2) wird nicht hier, sondern in
``app.schluessel.pruefe_schluesseltrennung_beim_start`` geprüft (eigenes Modul, weil sie
ohne Datenbank-Session auskommt) -- ``app.main`` ruft beide im selben Startgatter-Block
auf, hinter demselben ``enforce_startgatter``-Schalter. CRO-Nachforderung HED-42: die
reine Aufruf-bei-Versand-Prüfung in ``read_exchange_credential`` reichte nicht, weil eine
Installation ohne jeden Versand sie sonst nie ausgeführt hätte.
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


def pruefe_matcher_fuer_live_artefakt(run_matcher: bool) -> None:
    """Auflage 4, Startgatter 4 (CRO-Nachforderung HED-42): der Papier-Simulations-
    Matcher (``app.trading.matcher``, füllt Orders gegen simulierte Kurse) liegt nicht
    in ``app/trading/papier`` und wird deshalb von ``scripts/build_checks.py`` nicht aus
    dem Live-Artefakt entfernt. ``RUN_MATCHER`` ist ein eigener, von ARTEFAKT_ART
    unabhängiger Schalter (Default an) -- ohne diese Prüfung liefe der Papier-Matcher in
    einem Live-Artefakt unbemerkt parallel zum echten Orderpfad. Start verweigert, kein
    eingeschränkter Modus."""
    if lese_artefakt_art() == "live_artefakt" and run_matcher:
        raise StartgatterFehler(
            "RUN_MATCHER ist in einem Live-Artefakt gesetzt. Der Papier-Simulations-"
            "Matcher würde parallel zum echten Orderpfad laufen. Start verweigert "
            "(Auflage 4, Startgatter 4)."
        )
