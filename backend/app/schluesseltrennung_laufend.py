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
from app.marketdata.service import MarketService
from app.schluessel import selbsttest_bestanden
from app.trading.storno_sweep import sweep_ruhende_einstiegsorders
from app.trading.versandengpass import Versandengpass

log = logging.getLogger(__name__)

_BEGRUENDUNG = (
    "Laufende Schlüsseltrennungs-Prüfung (Selbsttest-Punkte 1-4, Abschnitt 3.2) "
    "während des Betriebs fehlgeschlagen."
)


async def pruefe_einmal(
    session_factory,
    engpass: Versandengpass | None = None,
    *,
    market: MarketService | None = None,
    tranche: str = "B1",
) -> bool:
    """Eine Durchführung des vollständigen Selbsttests (Punkte 1-4). Setzt bei
    Fehlschlag automatisch das firmenweite Sperrkennzeichen. Gibt zurück, ob der
    Selbsttest bestanden wurde.

    ``engpass``/``market`` (Pfad 2, HED-191 Abschnitt 1b): nur für den
    Storno-Sweep ruhender Einstiegsorders gebraucht, der unmittelbar nach dem Setzen
    ausgelöst wird -- ``engpass=None`` (z. B. ohne laufenden Matcher) lässt den
    Sweep auf seinen rein internen Z8-Anteil zurückfallen, siehe
    ``app.trading.storno_sweep``."""
    bestanden = await selbsttest_bestanden()
    if not bestanden:
        async with session_factory() as session:
            await setze_firmenweit(
                session,
                ausloeser="auflagen_selbsttest_fehlgeschlagen",
                begruendung=_BEGRUENDUNG,
            )
        try:
            await sweep_ruhende_einstiegsorders(
                session_factory, engpass, market=market, tranche=tranche
            )
        except Exception:
            log.exception("Storno-Sweep nach Schlüsseltrennungs-Fehlschlag fehlgeschlagen")
    return bestanden
