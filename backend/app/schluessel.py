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
"""

import os
from dataclasses import dataclass

from app.betriebsart import Betriebsart, get_betriebsart

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


def pruefe_schluesseltrennung_beim_start() -> None:
    """Start-Selbsttest (Auflage 2, Startgatter): die Querkontaminationsprüfung aus
    ``read_exchange_credential`` lief bisher nur beim ersten Versand -- also gar nicht,
    solange keine Order abgesetzt wird. Diese Funktion führt sie für jede Börse, deren
    Schlüssel überhaupt lesbar ist, bereits beim Prozessstart aus, damit ein auf dem
    falschen Host sichtbarer Schlüssel den Start verhindert statt erst den ersten
    Versand. Sofort-Stopp-Fall, kein eingeschränkter Start."""
    for boerse in _gefundene_boersen():
        read_exchange_credential(boerse)


def verifiziere_kontostand(credential: ExchangeCredential, exchange_client) -> bool:
    """Reiner Lesevorgang gegen die Gegenstelle (Selbsttest Punkt 4, Abschnitt 3.2).

    ``exchange_client`` wird injiziert, nicht hier konstruiert -- B-6 verdrahtet den
    echten ccxt-Client inklusive Header-Bindung, sobald das OKX-Demo-Secret vorliegt.
    Diese Funktion bindet nur zu: Ergebnis muss zur Betriebsart passen.
    """
    konto = exchange_client.fetch_balance()
    gemeldete_betriebsart = konto.get("betriebsart")
    return gemeldete_betriebsart == credential.betriebsart.value
