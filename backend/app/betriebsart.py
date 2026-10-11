"""Betriebsart (Auflage 1, Entwurf HED-36 Fassung 1.1 Abschnitt 3.1).

Papier und Live sind getrennte Installationen. Die Betriebsart unterscheidet sie und wird
genau einmal beim Prozessstart aus der Host-Umgebung gelesen. Es gibt danach keinen Pfad,
der sie zur Laufzeit umschaltet -- kein Konfigurationsparameter, kein Dashboard-Schalter,
kein Administrationsbefehl. ``get_betriebsart`` ist deshalb ``lru_cache``-gebunden: der
Prozess liest die Umgebungsvariable beim ersten Aufruf und danach nie wieder.
"""

import os
from enum import StrEnum
from functools import lru_cache


class Betriebsart(StrEnum):
    PAPIER = "papier"
    LIVE = "live"


class BetriebsartFehler(RuntimeError):
    """Startgatter-Fehler: die Installation startet nicht."""


@lru_cache
def get_betriebsart() -> Betriebsart:
    roh = os.environ.get("BETRIEBSART")
    try:
        return Betriebsart(roh)
    except ValueError as exc:
        raise BetriebsartFehler(
            f"BETRIEBSART muss 'papier' oder 'live' sein, nicht {roh!r}. "
            "Die Installation startet nicht (Entwurf HED-36 Abschnitt 3.1, Startgatter 1)."
        ) from exc


def ist_papier() -> bool:
    return get_betriebsart() is Betriebsart.PAPIER


def ist_live() -> bool:
    return get_betriebsart() is Betriebsart.LIVE
