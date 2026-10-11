"""Versand-Engpass (Auflage 3, Entwurf HED-36 Fassung 1.1 Abschnitt 3.3).

Genau ein Baustein übergibt Order an die Gegenstelle. Kein anderer Baustein ruft
``app.schluessel.read_exchange_credential`` auf oder spricht direkt mit der
Gegenstellen-Verbindung -- ``scripts/build_checks.py`` prüft das im Build, nicht nur
dieses Modul zur Laufzeit.

Die sieben Prüfschritte stehen in der im Entwurf festgelegten Reihenfolge. Schlägt einer
fehl, wird abgelehnt und protokolliert -- nicht angepasst, nicht verzögert, nicht später
erneut versucht.

Was hier bewusst NICHT gebaut ist: der volle Lebenszyklus der Sperrkennzeichen
(Zwei-Personen-Regel, automatisches Setzen, Benachrichtigung -- Kill-Switch-Zuschnitt-
Folgeissue), der echte Freigabe-Nachweis gegen das Proposal-Modell aus HED-41 (B-2-
Folgeissue, da HED-41 noch nicht gemergt ist) und der echte Netzwerkaufruf an die
Gegenstelle (B-6, wartet auf das OKX-Demo-Secret). Die fail-closed-Standardwerte unten
sorgen dafür, dass vor diesen Folgeissues technisch keine Order das Haus verlassen kann.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.betriebsart import get_betriebsart
from app.models.versandengpass import VersandEngpassEintrag
from app.schluessel import SchluesselFehler, read_exchange_credential
from app.sperrkennzeichen import doppelpruefung_offen

log = logging.getLogger(__name__)

ERLAUBTE_GATTUNGEN = frozenset(
    {
        "einstieg",
        "erhoehung",
        "stop_platzierung",
        "stop_nachfuehrung",
        "stornierung",
        "risikoabbau_stufe1",
        "risikoabbau_stufe2",
        "strategie_ausstieg",
        "not_liquidation",
    }
)

# Sperrwirkungstabelle, Entwurf Abschnitt 9.3: nur diese drei Gattungen hält ein
# gesetztes Kennzeichen an. Alles andere ist Risikoabbau oder reduziert Risiko und läuft
# unverändert weiter -- ein Kennzeichen, das das anhält, wäre der teuerste Einzelfehler.
GESPERRT_DURCH_KENNZEICHEN = frozenset({"einstieg", "erhoehung", "stop_nachfuehrung"})

BENOETIGT_FREIGABE_NACHWEIS = frozenset({"einstieg", "erhoehung"})


@dataclass(frozen=True)
class VersandOrder:
    gattung: str
    strategie_schluessel: str
    exchange: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    proposal_id: int | None = None
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class VersandErgebnis:
    angenommen: bool
    ablehnungsgrund: str | None
    gegenstellen_order_id: str | None


FreigabeNachweisPruefer = Callable[[AsyncSession, VersandOrder], Awaitable[bool]]
Stufe2Pruefer = Callable[[AsyncSession, VersandOrder], Awaitable[bool]]


async def _kein_freigabe_nachweis_verdrahtet(_session: AsyncSession, _order: VersandOrder) -> bool:
    """Fail-closed Standard. Echte Prüfung gegen Proposal.approval_token folgt mit B-2."""
    return False


async def _keine_stufe2_pruefung_verdrahtet(_session: AsyncSession, _order: VersandOrder) -> bool:
    """Fail-closed Standard. Echte Kursfrische-/Limit-/Preisband-Prüfung folgt mit B-2."""
    return False


def _order_referenz(order: VersandOrder) -> dict:
    return {
        "symbol": order.symbol,
        "side": order.side,
        "quantity": str(order.quantity),
        "order_type": order.order_type,
        "proposal_id": order.proposal_id,
        "limit_price": str(order.limit_price) if order.limit_price is not None else None,
    }


class Versandengpass:
    def __init__(
        self,
        freigabe_nachweis_pruefer: FreigabeNachweisPruefer = _kein_freigabe_nachweis_verdrahtet,
        stufe2_pruefer: Stufe2Pruefer = _keine_stufe2_pruefung_verdrahtet,
    ):
        self._freigabe_nachweis_pruefer = freigabe_nachweis_pruefer
        self._stufe2_pruefer = stufe2_pruefer
        self.zaehler: dict[str, int] = {}

    async def send(self, session: AsyncSession, order: VersandOrder) -> VersandErgebnis:
        grund = await self._pruefe(session, order)
        gegenstellen_order_id = None
        if grund is None:
            gegenstellen_order_id = await self._sende_an_gegenstelle(order)
        self.zaehler[order.gattung] = self.zaehler.get(order.gattung, 0) + 1
        session.add(
            VersandEngpassEintrag(
                gattung=order.gattung,
                strategie_schluessel=order.strategie_schluessel,
                exchange=order.exchange,
                angenommen=grund is None,
                ablehnungsgrund=grund,
                order_referenz=_order_referenz(order),
                gegenstellen_order_id=gegenstellen_order_id,
            )
        )
        await session.commit()
        return VersandErgebnis(grund is None, grund, gegenstellen_order_id)

    async def _pruefe(self, session: AsyncSession, order: VersandOrder) -> str | None:
        # Schritt 1: Betriebsart des Prozesses == Betriebsart-Markierung des Schlüssels.
        # Dies ist der einzige Aufrufer von read_exchange_credential in der Installation
        # (Build-Prüfung in scripts/build_checks.py erzwingt das).
        try:
            credential = read_exchange_credential(order.exchange)
        except SchluesselFehler as exc:
            log.error("Versand-Engpass: Schlüssel nicht verwendbar: %s", exc)
            return "schluessel_nicht_verwendbar"
        if credential.betriebsart != get_betriebsart():
            return "betriebsart_schluessel_mismatch"

        # Schritt 2: Ordergattung muss bekannt sein.
        if order.gattung not in ERLAUBTE_GATTUNGEN:
            return "unbekannte_gattung"

        # Schritt 3-5: Doppelprüfung der Sperrkennzeichen, nur für die betroffenen
        # Gattungen (Sperrwirkungstabelle, Abschnitt 9.3). Fail closed bei Lesefehler.
        if order.gattung in GESPERRT_DURCH_KENNZEICHEN:
            if not await doppelpruefung_offen(session, order.strategie_schluessel):
                return "unterdrueckt_durch_sperre"

        # Schritt 6: Freigabe-Nachweis für Gattungen, die Board-Freigabe brauchen.
        if order.gattung in BENOETIGT_FREIGABE_NACHWEIS:
            if not await self._freigabe_nachweis_pruefer(session, order):
                return "kein_gueltiger_freigabe_nachweis"

        # Schritt 7: Stufe-2-Nachprüfung, für jede Gattung ohne Ausnahme.
        if not await self._stufe2_pruefer(session, order):
            return "stufe2_nicht_bestanden"

        return None

    async def _sende_an_gegenstelle(self, order: VersandOrder) -> str:
        """Die einzige Stelle, die je einen echten Netzwerkaufruf an die Gegenstelle
        machen darf. Noch nicht verdrahtet: B-6 baut den OKX-Orderpfad, sobald das
        OKX-Demo-Secret vorliegt. Bis dahin kann -- selbst wenn alle sieben Prüfungen
        bestehen -- keine Order das Haus verlassen.
        """
        raise NotImplementedError(
            f"B-6 (OKX-Orderpfad) ist noch nicht gebaut. Order für {order.exchange} "
            f"{order.symbol} hat alle Prüfungen bestanden, aber es gibt noch keinen "
            "Code, der sie absenden könnte."
        )
