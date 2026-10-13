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
from decimal import Decimal
from typing import Protocol

import ccxt
import ccxt.async_support as ccxt_async

from app.config import get_settings
from app.schluessel import ExchangeCredential

QUELLE = "okx_demo"
OKX_DEMO_TIMEOUT_MS = 10_000
_UNTERSTUETZTE_ORDERTYPEN = frozenset({"market", "limit"})


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


@dataclass(frozen=True)
class KontostandErgebnis:
    quelle: str
    betriebsart: str
    rohdaten: dict


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
        client.headers = {**(client.headers or {}), "x-simulated-trading": "1"}
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
            f"Ordertyp {order.order_type!r} wird vom OKX-Demo-Orderpfad (B-6) noch nicht "
            "unterstützt -- Stop-Orders folgen mit B-12 (Stop als börsenseitige "
            "Stop-Market-Order)."
        )
    client = _client(credential)
    try:
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
