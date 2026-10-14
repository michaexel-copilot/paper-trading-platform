"""OKX-Demo-Orderpfad (B-6, HED-76), Papier-spezifischer Code (Auflage 4, Abschnitt 3.4).

Diese Datei liegt in ``app/trading/papier`` und ist damit im Live-Artefakt **nicht**
enthalten (``scripts/build_checks.py``). Sie ist der einzige Ort, der tatsächlich über
das Netz mit OKX-Demo spricht. Aufgerufen wird sie ausschließlich vom Versand-Engpass
(``app.trading.versandengpass._sende_an_gegenstelle``), der die Zugangsdaten bereits
über ``read_exchange_credential`` gelesen hat und hier als Parameter übergibt -- dieses
Modul ruft ``read_exchange_credential`` selbst nicht auf, sonst gäbe es eine zweite
Zugriffsstelle und der Build bräche ab (Auflage 3).

Isolationspflicht (HED-76 Punkt 3): jede Antwort trägt die Kennzeichnung
``quelle="okx_demo"`` -- getrennt von der Betriebsart ("papier"/"live"), weil OKX-Demo-
Preise 1,41 % neben dem Live-Markt liegen (Handbuch Abschnitt 7) und nie unmarkiert in
einem Performance- oder Renditedokument landen dürfen. Dieses Modul schreibt nicht in
die Datenbank und importiert bewusst keines der Modelle/Module, die den Ergebnis-
/Performance-Speicher der Paper-Trading-Plattform ausmachen (``app.models``,
``app.trading.valuation``, ``app.trading.matcher``, ``app.trading.engine``,
``app.trading.orders``) -- ``scripts/build_checks.py`` erzwingt das maschinell.

Fehlerverhalten (HED-76 Punkt 4): Timeout, Ablehnung durch die Börse und ein falsches
Kontostand-Format sind drei eigene Ausnahmen. Keine davon fängt sich selbst auf und
versucht leiser etwas anderes -- das wäre der verbotene stille Fallback.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

import ccxt
import ccxt.async_support as ccxt_async

from app.config import get_settings
from app.dbtypes import utcnow
from app.schluessel import ExchangeCredential

QUELLE = "okx_demo"
OKX_DEMO_TIMEOUT_MS = 10_000
_UNTERSTUETZTE_ORDERTYPEN = frozenset({"market", "limit", "stop_market"})

# B-12 (HED-84, Entwurf Abschnitt 6.2): OKX/ccxt-Rohstatus auf die vier Werte
# normalisiert, die die Fill-Erkennung kennt. "canceled" zählt zu "beendet", nicht zu
# "abgelehnt" -- eine stornierte Order ist keine Ablehnung durch die Gegenstelle,
# sondern i. d. R. unser eigener Fristwächter oder eine manuelle Aktion; die
# Fill-Erkennung behandelt "beendet" mit gefuellte_menge > 0 als Z12 (Entwurf Tabelle
# 6.2) und mit gefuellte_menge == 0 identisch zu "abgelehnt" (keine Position).
_STATUS_NORMALISIERT = {
    "open": "offen",
    "closed": "gefuellt",
    "canceled": "beendet",
    "expired": "beendet",
    "rejected": "abgelehnt",
}


class OkxDemoFehler(RuntimeError):
    """Basisklasse. Jeder Fehlerfall ist definiert -- kein stiller Fallback (Punkt 4)."""


class OkxDemoTimeout(OkxDemoFehler):
    """OKX-Demo antwortet nicht innerhalb der Zeitgrenze."""


class OkxDemoAblehnung(OkxDemoFehler):
    """OKX-Demo lehnt die Anfrage ab oder meldet einen Börsenfehler (Order oder Kontostand)."""


class OkxDemoAntwortFehler(OkxDemoFehler):
    """OKX-Demo antwortet formal, aber nicht im erwarteten Format."""


class _OrderWie(Protocol):
    """Strukturelle Mindestform, die ``VersandOrder`` erfüllt -- kein Import von
    ``app.trading.versandengpass`` hier, damit dieses Modul eigenständig bleibt."""

    exchange: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal | None
    stop_price: Decimal | None  # B-12 (HED-84): nur für order_type == "stop_market"


@dataclass(frozen=True)
class KontostandErgebnis:
    quelle: str
    betriebsart: str
    rohdaten: dict


@dataclass(frozen=True)
class OrderStatusErgebnis:
    """B-12 (HED-84, Entwurf Abschnitt 12.1): Rückgabe von ``lese_order_status``.
    Bewusst ohne Preisfeld -- der Stop-Kurs stammt aus dem freigegebenen Vorschlag
    (``Proposal.stop_price``), nicht von der Börse (Entwurf Abschnitt 6.3)."""

    quelle: str
    status: str  # offen | teilgefuellt | gefuellt | beendet | abgelehnt
    gefuellte_menge: Decimal
    restmenge: Decimal
    zeitstempel: datetime


@dataclass(frozen=True)
class OffeneAlgoOrder:
    """B-12 (HED-84, Entwurf Abschnitt 12.1): eine Zeile aus ``lese_offene_algo_orders``."""

    quelle: str
    algo_id: str
    symbol: str
    side: str
    menge: Decimal
    trigger_kurs: Decimal
    zeitstempel: datetime


def _normalisiere_order_status(
    roh_status: str, gefuellte_menge: Decimal, restmenge: Decimal
) -> str:
    """B-12 (Entwurf Abschnitt 6.2/12.1): OKX/ccxt-Rohstatus auf die fünf von der
    Fill-Erkennung erwarteten Werte normalisiert.

    ``canceled``/``expired`` heißen hier "beendet", unabhängig von der Füllmenge -- die
    Fill-Erkennung (``app.trading.stop_pflicht``) unterscheidet danach selbst zwischen
    "beendet mit Füllung" (Z12, Stop über die Fill-Größe) und "beendet ohne Füllung"
    (keine Position, wie Z13). Diese Funktion trifft diese Unterscheidung bewusst nicht
    selbst -- sie normalisiert nur den Rohstatus, die Zustandsentscheidung bleibt beim
    Aufrufer (gleiche Trennung wie überall in diesem Modul: lesen hier, entscheiden dort).
    """
    if roh_status == "rejected":
        return "abgelehnt"
    if roh_status in ("canceled", "expired"):
        return "beendet"
    if roh_status == "closed":
        return "gefuellt"
    if roh_status == "open":
        if gefuellte_menge <= 0:
            return "offen"
        if restmenge <= 0:
            return "gefuellt"
        return "teilgefuellt"
    raise OkxDemoAntwortFehler(f"Unbekannter Order-Status von OKX-Demo: {roh_status!r}")


def _zeitstempel_aus_ms(wert) -> datetime:
    if wert is None:
        return utcnow()
    from datetime import UTC

    return datetime.fromtimestamp(int(wert) / 1000, tz=UTC)


def _client(credential: ExchangeCredential) -> ccxt_async.okx:
    if credential.exchange != "okx":
        raise OkxDemoFehler(
            f"OKX-Demo-Orderpfad unterstützt nur exchange='okx', nicht {credential.exchange!r}."
        )
    client = ccxt_async.okx(
        {
            "apiKey": credential.api_key,
            "secret": credential.api_secret,
            "password": credential.passphrase,
            "enableRateLimit": True,
            "timeout": OKX_DEMO_TIMEOUT_MS,
            # HED-87: der OKX-Demo-Schlüssel des Boards existiert nur auf eea.okx.com
            # (OKX-EWR-Einheit) -- konfigurierbar statt fest verdrahtet, siehe
            # app.config.Settings.okx_demo_hostname.
            "hostname": get_settings().okx_demo_hostname,
        }
    )
    if credential.simulated_trading_header:
        # HED-92: ccxt/OKX sehen für den Demo-Modus eine eigene Sperre vor --
        # fetch_currencies() (GET /api/v5/asset/currencies, Fehler 50038 im Demo-
        # Modus) übersetzt automatisch {}, wenn self.options['sandboxMode'] gesetzt
        # ist (ccxt 4.5.85, okx.py:2030-2032). set_sandbox_mode(True) setzt genau
        # dieses Flag und zusätzlich denselben Header, den wir bisher händisch
        # gesetzt haben -- load_markets()/create_order() laufen damit durch, ohne
        # den gesperrten Endpunkt zu berühren. Der von der Basisklasse sonst
        # vorgenommene URL-Wechsel ist für OKX folgenlos: urls['test']['rest'] und
        # urls['api']['rest'] sind identisch ('https://{hostname}'); der
        # konfigurierbare Hostname aus HED-87 bleibt unberührt.
        client.set_sandbox_mode(True)
    return client


async def platziere_order(credential: ExchangeCredential, order: _OrderWie) -> str:
    """Sendet genau eine Order an OKX-Demo und gibt die Gegenstellen-Order-Id zurück.

    Aufgerufen ausschließlich vom Versand-Engpass, nachdem alle sieben Prüfschritte
    bestanden sind. Wirft bei jedem Fehlerfall eine eigene, nicht aufgefangene Ausnahme
    -- der Versand-Engpass protokolliert diesen Entscheid (noch) nicht als
    ``VersandEngpassEintrag``; der wiederkehrende Abgleich gegen die Gegenstellen-
    Ordernachweise (B-5) ist das Sicherheitsnetz für diesen Fall.
    """
    if order.order_type not in _UNTERSTUETZTE_ORDERTYPEN:
        raise OkxDemoAblehnung(
            f"Ordertyp {order.order_type!r} wird vom OKX-Demo-Orderpfad (B-6/B-12) nicht "
            "unterstützt."
        )
    if order.order_type == "stop_market" and order.limit_price is not None:
        # F-12, Ebene 3 (Entwurf Abschnitt 8.2): Abbruch VOR jedem Netzaufruf. Ein
        # gesetzter limit_price würde über ccxt's `slOrdPx = safe_number(params,
        # 'slOrdPx', price)` zum Limitpreis des Stops -- aus dem Pflicht-Stop würde
        # eine Stop-Limit-Order, die HED-8 verbietet, ohne dass OKX das zurückweist.
        raise OkxDemoAblehnung(
            "Stop-Market-Order mit gesetztem limit_price ist strukturell verboten "
            "(Falle 1, Entwurf Abschnitt 8.2) -- ein Programmierfehler beim Aufrufer, "
            "kein Börsenzustand. Kein Netzaufruf erfolgt."
        )
    client = _client(credential)
    try:
        if order.order_type == "stop_market":
            # F-12, Ebenen 1+2: `price` fest als None (Literal, keine Variable, die
            # einen Limitpreis einschleusen könnte); `params` enthält nie `slOrdPx`.
            # `tdMode: "cash"` erzwingt Spot-Kassa-Modus (Falle 2, Entwurf Abschnitt
            # 8.3) -- ccxt überschreibt tdMode für bedingte Order sonst mit
            # `marginMode` (Standard "cross"), wird aber durch den hier gesetzten
            # Zusatzparameter beim abschließenden `self.extend(request, orderParams)`
            # wieder auf "cash" zurückgesetzt (empirisch gegen ccxt 4.5.88 geprüft,
            # Prüfplan P-4a).
            try:
                antwort = await client.create_order(
                    order.symbol,
                    "market",
                    order.side,
                    float(order.quantity),
                    None,
                    {"stopLossPrice": float(order.stop_price), "tdMode": "cash"},
                )
            except ccxt.RequestTimeout as exc:
                raise OkxDemoTimeout(
                    f"OKX-Demo antwortet nicht auf Stop-Order-Versuch {order.symbol}: {exc}"
                ) from exc
            except ccxt.BaseError as exc:
                raise OkxDemoAblehnung(
                    f"OKX-Demo lehnt Stop-Order ab ({order.symbol} {order.side} "
                    f"{order.quantity} @ {order.stop_price}): {exc}"
                ) from exc
        else:
            preis = float(order.limit_price) if order.limit_price is not None else None
            try:
                antwort = await client.create_order(
                    order.symbol, order.order_type, order.side, float(order.quantity), preis
                )
            except ccxt.RequestTimeout as exc:
                raise OkxDemoTimeout(
                    f"OKX-Demo antwortet nicht auf Orderversuch {order.symbol}: {exc}"
                ) from exc
            except ccxt.BaseError as exc:
                raise OkxDemoAblehnung(
                    f"OKX-Demo lehnt Order ab ({order.symbol} {order.side} {order.quantity}): {exc}"
                ) from exc
    finally:
        await client.close()
    order_id = antwort.get("id") if isinstance(antwort, dict) else None
    if not order_id:
        raise OkxDemoAntwortFehler(f"OKX-Demo-Antwort ohne Order-Id: {antwort!r}")
    return order_id


async def lese_order_status(
    credential: ExchangeCredential, order_id: str, symbol: str
) -> OrderStatusErgebnis:
    """Reiner Lesevorgang (B-12, Entwurf Abschnitt 6.2/8.5). Aufgerufen ausschließlich
    aus ``Versandengpass.lese_order_status`` (F-1) -- dieses Modul liest selbst keinen
    Schlüssel."""
    client = _client(credential)
    try:
        try:
            antwort = await client.fetch_order(order_id, symbol)
        except ccxt.RequestTimeout as exc:
            raise OkxDemoTimeout(
                f"OKX-Demo antwortet nicht auf Order-Status-Abfrage {symbol}/{order_id}: {exc}"
            ) from exc
        except ccxt.BaseError as exc:
            raise OkxDemoAblehnung(
                f"OKX-Demo lehnt Order-Status-Abfrage ab ({symbol}/{order_id}): {exc}"
            ) from exc
    finally:
        await client.close()
    if not isinstance(antwort, dict):
        raise OkxDemoAntwortFehler(f"Unerwartetes Order-Status-Format von OKX-Demo: {antwort!r}")
    roh_status = antwort.get("status")
    roh_gefuellt = antwort.get("filled")
    if roh_status is None or roh_gefuellt is None:
        raise OkxDemoAntwortFehler(f"Unerwartetes Order-Status-Format von OKX-Demo: {antwort!r}")
    gefuellte_menge = Decimal(str(roh_gefuellt))
    roh_rest = antwort.get("remaining")
    restmenge = Decimal(str(roh_rest)) if roh_rest is not None else Decimal(0)
    status = _normalisiere_order_status(roh_status, gefuellte_menge, restmenge)
    return OrderStatusErgebnis(
        quelle=QUELLE,
        status=status,
        gefuellte_menge=gefuellte_menge,
        restmenge=restmenge,
        zeitstempel=_zeitstempel_aus_ms(antwort.get("lastUpdateTimestamp")),
    )


async def lese_offene_algo_orders(
    credential: ExchangeCredential, symbol: str
) -> list[OffeneAlgoOrder]:
    """Reiner Lesevorgang (B-12, Entwurf Abschnitt 8.5/10.3): offene OKX-Algo-Order für
    den täglichen Orderbuch-Abgleich. Aufgerufen ausschließlich aus
    ``Versandengpass.lese_offene_stop_orders`` (F-1)."""
    client = _client(credential)
    try:
        try:
            antwort = await client.fetch_open_orders(symbol, params={"ordType": "conditional"})
        except ccxt.RequestTimeout as exc:
            raise OkxDemoTimeout(
                f"OKX-Demo antwortet nicht auf Abfrage offener Stop-Order {symbol}: {exc}"
            ) from exc
        except ccxt.BaseError as exc:
            raise OkxDemoAblehnung(
                f"OKX-Demo lehnt Abfrage offener Stop-Order ab ({symbol}): {exc}"
            ) from exc
    finally:
        await client.close()
    if not isinstance(antwort, list):
        raise OkxDemoAntwortFehler(f"Unerwartetes Algo-Order-Format von OKX-Demo: {antwort!r}")
    ergebnisse = []
    for eintrag in antwort:
        algo_id = eintrag.get("id")
        side = eintrag.get("side")
        menge = eintrag.get("amount")
        # Unsere Order sind ausschließlich stopLoss-bedingte Order (Entwurf Abschnitt
        # 8.1) -- der Triggerkurs steht dort in `stopLossPrice`, nicht in
        # `triggerPrice` (das ist der Gattung "trigger" vorbehalten, hier nicht
        # verwendet).
        trigger = eintrag.get("stopLossPrice")
        if not algo_id or side is None or menge is None or trigger is None:
            raise OkxDemoAntwortFehler(f"Unerwartetes Algo-Order-Format von OKX-Demo: {eintrag!r}")
        ergebnisse.append(
            OffeneAlgoOrder(
                quelle=QUELLE,
                algo_id=str(algo_id),
                symbol=symbol,
                side=side,
                menge=Decimal(str(menge)),
                trigger_kurs=Decimal(str(trigger)),
                zeitstempel=_zeitstempel_aus_ms(eintrag.get("lastUpdateTimestamp")),
            )
        )
    return ergebnisse


async def storniere_algo_order(credential: ExchangeCredential, algo_id: str, symbol: str) -> str:
    """Stornierung einer Stop-Order (B-12, Gattung "stornierung", Entwurf Abschnitt
    8.5). Aufgerufen ausschließlich aus ``Versandengpass._sende_an_gegenstelle``, nach
    denselben sieben Prüfschritten wie jede andere Order -- kein Sonderweg."""
    client = _client(credential)
    try:
        try:
            antwort = await client.cancel_order(algo_id, symbol, params={"trigger": True})
        except ccxt.RequestTimeout as exc:
            raise OkxDemoTimeout(
                f"OKX-Demo antwortet nicht auf Stornierung {symbol}/{algo_id}: {exc}"
            ) from exc
        except ccxt.BaseError as exc:
            raise OkxDemoAblehnung(
                f"OKX-Demo lehnt Stornierung ab ({symbol}/{algo_id}): {exc}"
            ) from exc
    finally:
        await client.close()
    storno_id = antwort.get("id") if isinstance(antwort, dict) else None
    if not storno_id:
        raise OkxDemoAntwortFehler(f"OKX-Demo-Stornierungsantwort ohne Order-Id: {antwort!r}")
    return storno_id


async def lese_kontostand(credential: ExchangeCredential) -> KontostandErgebnis:
    """Reiner Lesevorgang gegen den Kontostand (HED-76 Punkt 2, für die Schlüssel-
    trennungs-Verifikation aus HED-55). Setzt keine Order ab.

    HED-90: ``ccxt``s vereinheitlichtes ``fetch_balance()`` lädt zuerst die Märkte
    (``load_markets``), und das lädt bei OKX nebenbei ``fetch_currencies()`` --
    ``GET /api/v5/asset/currencies``. Dieser Funding-Endpunkt ist im Demo-Modus
    gesperrt und liefert OKX-Fehler 50038 ("This feature is unavailable in demo
    trading"), bevor der eigentliche Kontostand-Aufruf überhaupt stattfindet
    (Board-Handtest 2026-10-13: ``GET /api/v5/account/balance`` direkt funktioniert
    im Demo-Modus). Deshalb ruft dieser Lesevorgang die implizite ccxt-Methode
    ``privateGetAccountBalance`` auf -- das ist genau ``GET /api/v5/account/balance``,
    ohne den Zwischenaufruf über ``load_markets``. ``rohdaten`` ist damit die
    rohe OKX-Antwort (``code``/``data``/``msg``), nicht mehr ccxt's vereinheitlichte
    Bilanzstruktur.
    """
    client = _client(credential)
    try:
        try:
            rohdaten = await client.privateGetAccountBalance()
        except ccxt.RequestTimeout as exc:
            raise OkxDemoTimeout(f"OKX-Demo antwortet nicht auf Kontostand-Abfrage: {exc}") from exc
        except ccxt.BaseError as exc:
            raise OkxDemoAblehnung(f"OKX-Demo lehnt Kontostand-Abfrage ab: {exc}") from exc
    finally:
        await client.close()
    if not isinstance(rohdaten, dict) or not rohdaten.get("data"):
        raise OkxDemoAntwortFehler(
            f"Unerwartetes Kontostand-Format von OKX-Demo: {type(rohdaten).__name__}"
        )
    return KontostandErgebnis(
        quelle=QUELLE, betriebsart=credential.betriebsart.value, rohdaten=rohdaten
    )
