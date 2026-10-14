"""Laufende Prüfung der Schlüsseltrennung während des Betriebs (HED-55, Entwurf
HED-36 Fassung 1.1 Abschnitt 3.2, "Laufende Prüfung, nicht nur beim Start").

Der Start-Selbsttest (``app.schluessel.pruefe_schluesseltrennung_beim_start``) lässt
den Prozess gar nicht erst anlaufen, wenn er fehlschlägt. Während des laufenden
Betriebs ist ein Absturz keine Option mehr -- stattdessen geht der Prozess in den
Nur-Risikoabbau-Zustand: das firmenweite Sperrkennzeichen wird automatisch gesetzt
(Abschnitt 9.5, Auslöser ``auflagen_selbsttest_fehlgeschlagen``, bereits in
``app.kill_switch.FIRMENWEIT_SETZEN_AUSLOESER`` vorgesehen). Die Sperrwirkungstabelle
(Abschnitt 9.3) hält dadurch nur ``einstieg``, ``erhoehung`` und ``stop_nachfuehrung``
an -- laufender Risikoabbau ist unverändert davon ausgenommen, das ist der
Nur-Risikoabbau-Zustand in dieser Installation.

Dieses Modul hält nur die einzelne, testbare Durchführung (``pruefe_einmal``) -- die
Endlos-Schleife mit Intervall lebt in ``app.main``, genauso wie bei
``check_all_assets`` / ``verfalle_ueberfaellige_vorschlaege``.
"""

import logging

from app.kill_switch import setze_firmenweit
from app.schluessel import selbsttest_bestanden

log = logging.getLogger(__name__)

_BEGRUENDUNG = (
    "Laufende Schlüsseltrennungs-Prüfung (Selbsttest-Punkte 1-4, Abschnitt 3.2) "
    "während des Betriebs fehlgeschlagen."
)


async def pruefe_einmal(session_factory) -> bool:
    """Eine Durchführung des vollständigen Selbsttests (Punkte 1-4). Setzt bei
    Fehlschlag automatisch das firmenweite Sperrkennzeichen. Gibt zurück, ob der
    Selbsttest bestanden wurde."""
    bestanden = await selbsttest_bestanden()
    if not bestanden:
        async with session_factory() as session:
            await setze_firmenweit(
                session,
                ausloeser="auflagen_selbsttest_fehlgeschlagen",
                begruendung=_BEGRUENDUNG,
            )
    return bestanden
