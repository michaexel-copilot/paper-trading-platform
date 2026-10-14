"""Schlüsseltrennung (Auflage 2, Entwurf HED-36 Fassung 1.1 Abschnitt 3.2).

Es gibt in der ganzen Installation genau diese eine Funktion, die einen Gegenstellen-
Schlüssel liest: ``read_exchange_credential``. Kein anderer Baustein liest Umgebungs-
variablen mit Schlüsselinhalt direkt. Der Versand-Engpass (``app.trading.versandengpass``)
ist der einzige Aufrufer -- das wird von ``scripts/build_checks.py`` erzwungen, nicht nur
behauptet.

Jeder Schlüssel ist über die Namenskonvention ``{BOERSE}_{DEMO|LIVE}_...`` an eine
Betriebsart gebunden. Diese Funktion prüft bei jedem Aufruf zuerst, ob ein Schlüssel der
JEWEILS ANDEREN Betriebsart lesbar ist -- das ist das benannte Hauptrisiko in Reinform
und führt unabhängig vom eigentlichen Leseergebnis zum Abbruch.

Bekannte Einschränkung, nicht von diesem Modul lösbar: OKX' Demo-Handel läuft über
denselben Hostnamen wie das Live-Konto, unterschieden nur durch den Header
``x-simulated-trading``, nicht durch eine eigene Netzadresse. Die in Abschnitt 3.2
Schicht 4 verlangte Netzroute Papier-Host -> Live-Endpunkt existiert für OKX damit nicht
als trennbare Netzadresse. Diese Datei erzwingt die Trennung deshalb ausschließlich über
Schicht 2 und 3 (Betriebsart-Markierung des Schlüssels, Endpunkt-/Header-Bindung) und
dokumentiert Schicht 4 für OKX als offene Frage an CRO/CEO (siehe Issue-Kommentar).

HED-55 ergänzt Selbsttest-Punkt 4 (Abschnitt 3.2: "Der Schlüssel wird gegen die
Gegenstelle mit einem reinen Lesevorgang (Kontostand) verifiziert. Das Ergebnis muss
zur Betriebsart passen.") -- bisher deckte der Start-Selbsttest nur die
Querkontaminationsprüfung (Punkte 1-3) ab, weil ``_sende_an_gegenstelle`` bis B-6
(HED-76) ``NotImplementedError`` warf und es keinen echten Lesevorgang gab.
``verifiziere_kontostand`` ist jetzt auf ``app.trading.papier.okx_demo.lese_kontostand``
verdrahtet (Papier) bzw. hat für Live bewusst keine Anbindung (noch kein Live-Orderpfad,
noch kein Live-Schlüssel) -- Punkt 4 kann für Live deshalb nicht bestehen, was
erzwungen fail closed ist, nicht ein Versehen. Der Import steht deferred in
``_echter_kontostand_leser``, aus demselben Grund wie in
``app.trading.versandengpass._sende_an_gegenstelle``: ``app/trading/papier`` ist im
Live-Artefakt nicht vorhanden (Auflage 4).
"""

import logging
import os
from dataclasses import dataclass

from app.betriebsart import Betriebsart, get_betriebsart

log = logging.getLogger(__name__)

_TAG = {Betriebsart.PAPIER: "DEMO", Betriebsart.LIVE: "LIVE"}


class SchluesselFehler(RuntimeError):
    """Sofort-Stopp-Fall: Schlüssel-Betriebsart passt nicht, fehlt, oder beides ist lesbar."""


@dataclass(frozen=True)
class ExchangeCredential:
    exchange: str
    api_key: str
    api_secret: str
    passphrase: str | None
    betriebsart: Betriebsart
    # True für OKX-Demo: muss bei jedem Aufruf an die Gegenstelle mitgeschickt werden.
    simulated_trading_header: bool


def _env(exchange: str, tag: str, feld: str) -> str | None:
    return os.environ.get(f"{exchange.upper()}_{tag}_{feld}")


def read_exchange_credential(exchange: str) -> ExchangeCredential:
    betriebsart = get_betriebsart()
    eigenes_tag = _TAG[betriebsart]
    ist_papier = betriebsart is Betriebsart.PAPIER
    fremdes_tag = _TAG[Betriebsart.LIVE if ist_papier else Betriebsart.PAPIER]

    if _env(exchange, fremdes_tag, "API_KEY") is not None:
        raise SchluesselFehler(
            f"Ein als {fremdes_tag} markierter Schlüssel für {exchange} ist lesbar, "
            f"während der Prozess als {betriebsart.value} läuft. Sofort-Stopp-Fall "
            "(Auflage 2, Hauptrisiko)."
        )

    api_key = _env(exchange, eigenes_tag, "API_KEY")
    api_secret = _env(exchange, eigenes_tag, "API_SECRET")
    if not api_key or not api_secret:
        raise SchluesselFehler(
            f"Kein {eigenes_tag}-Schlüssel für {exchange} lesbar. Start verweigert "
            "(Auflage 2, Startgatter)."
        )
    passphrase = _env(exchange, eigenes_tag, "PASSPHRASE")

    return ExchangeCredential(
        exchange=exchange,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        betriebsart=betriebsart,
        simulated_trading_header=(betriebsart is Betriebsart.PAPIER),
    )


def _gefundene_boersen() -> set[str]:
    """Jede Börse, für die überhaupt ein Schlüssel (gleich welcher Betriebsart) in der
    Umgebung lesbar ist -- unabhängig davon, ob diese Installation die Börse heute
    schon über den Versand-Engpass anspricht."""
    boersen = set()
    for name in os.environ:
        for tag in _TAG.values():
            suffix = f"_{tag}_API_KEY"
            if name.endswith(suffix):
                boersen.add(name[: -len(suffix)].lower())
    return boersen


async def _echter_kontostand_leser(credential: ExchangeCredential):
    """Echter Lesevorgang gegen die Gegenstelle für Selbsttest-Punkt 4.

    Deferred Import wie in ``app.trading.versandengpass._sende_an_gegenstelle``:
    ``app.trading.papier`` ist im Live-Artefakt nicht vorhanden (Auflage 4) -- ein
    Import am Dateikopf würde den Start dort brechen, selbst wenn dieser Zweig nie
    erreicht wird. Gibt ``None`` zurück, wenn kein Lesevorgang existiert (Live) oder
    die Gegenstelle ablehnt -- beides ist für ``verifiziere_kontostand`` derselbe
    Fehlschlag, kein stiller Fallback.
    """
    if credential.betriebsart is not Betriebsart.PAPIER:
        log.error(
            "Kein Live-Kontostand-Lesevorgang für %s gebaut -- Selbsttest-Punkt 4 kann "
            "für Live nicht bestehen (kein Live-Orderpfad, kein Live-Schlüssel).",
            credential.exchange,
        )
        return None

    from app.trading.papier.okx_demo import OkxDemoFehler, lese_kontostand

    try:
        return await lese_kontostand(credential)
    except OkxDemoFehler:
        log.exception("Kontostand-Lesevorgang gegen %s fehlgeschlagen.", credential.exchange)
        return None


async def verifiziere_kontostand(credential: ExchangeCredential) -> bool:
    """Reiner Lesevorgang gegen die Gegenstelle (Selbsttest Punkt 4, Abschnitt 3.2).
    Ergebnis muss zur Betriebsart passen. Ein fehlgeschlagener Lesevorgang zählt
    ebenfalls als nicht bestanden -- fail closed, kein Unterschied zwischen "Antwort
    passt nicht" und "keine Antwort" für den Aufrufer.
    """
    ergebnis = await _echter_kontostand_leser(credential)
    if ergebnis is None:
        return False
    return ergebnis.betriebsart == credential.betriebsart.value


async def _pruefe_alle_boersen() -> None:
    """Punkte 1-4 für jede Börse, deren Schlüssel überhaupt lesbar ist (siehe
    ``_gefundene_boersen``). Wirft ``SchluesselFehler`` bei jedem Fehlschlag --
    Querkontamination (1-3) wie bisher, jetzt zusätzlich die Kontostand-Verifikation
    (4, HED-55)."""
    for boerse in _gefundene_boersen():
        credential = read_exchange_credential(boerse)
        if not await verifiziere_kontostand(credential):
            raise SchluesselFehler(
                f"Kontostand-Verifikation für {boerse} ({credential.betriebsart.value}) "
                "fehlgeschlagen oder das Ergebnis passt nicht zur Betriebsart. "
                "Sofort-Stopp-Fall (Selbsttest-Punkt 4, Auflage 2)."
            )


async def pruefe_schluesseltrennung_beim_start() -> None:
    """Start-Selbsttest (Auflage 2, Startgatter): die Querkontaminationsprüfung aus
    ``read_exchange_credential`` lief bisher nur beim ersten Versand -- also gar nicht,
    solange keine Order abgesetzt wird. Diese Funktion führt sie für jede Börse, deren
    Schlüssel überhaupt lesbar ist, bereits beim Prozessstart aus, damit ein auf dem
    falschen Host sichtbarer Schlüssel den Start verhindert statt erst den ersten
    Versand. Seit HED-55 zusätzlich Punkt 4 (Kontostand-Verifikation). Sofort-Stopp-
    Fall, kein eingeschränkter Start."""
    await _pruefe_alle_boersen()


async def selbsttest_bestanden() -> bool:
    """Dieselben Punkte 1-4 für die laufende Prüfung während des Betriebs (Abschnitt
    3.2, "Laufende Prüfung, nicht nur beim Start"). Anders als der Start-Selbsttest
    wirft diese Funktion nicht -- der Aufrufer (``app.schluesseltrennung_laufend``)
    entscheidet über die Konsequenz (Sperrkennzeichen setzen), nicht dieses Modul,
    das weiterhin ohne Datenbank-Session auskommt."""
    try:
        await _pruefe_alle_boersen()
    except SchluesselFehler:
        log.error("Laufende Schlüsseltrennungs-Prüfung (Punkte 1-4) fehlgeschlagen.")
        return False
    return True
