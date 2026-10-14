"""Echter Netzzugriff gegen OKX-Demo für die Stop-Market-Technik aus B-12 (HED-84,
Entwurf Abschnitt 8, Prüfplan Abschnitt 13.2).

Wie ``test_okx_demo_live.py``: läuft nicht in der normalen Testrunde (``addopts = "-m
'not live' -q"``), nur explizit über ``pytest -m live``. Ohne OKX-Demo-Secret wird
übersprungen, nicht als Fehlschlag gewertet. Keine dieser Messungen ist eine
Performance- oder Renditemessung (Handbuch Abschnitt 7) -- es geht ausschließlich um
die technische Abnahme des Orderpfads.

**Abbruchkriterium P-4b** (Entwurf 13.2): schlägt dieser Test fehl, ist eine
börsenseitige Spot-Stop-Market-Order im freigegebenen Umfang (ohne Margin) nicht
umsetzbar -- dann dem CRO einen geänderten Entwurf vorlegen, nicht improvisieren.

Gemessen am 14.10.2026 gegen eea.okx.com (OKX-Demo, ccxt 4.5.88): P-4b und P-13
bestanden -- ``ordType=conditional``, ``slOrdPx=-1``, ``tdMode=cash``,
``slTriggerPx`` wie gesendet; die Gegenprobe mit explizitem ``slOrdPx`` liefert einen
echten Limitpreis statt ``-1``. Beleg in der Teilaufgabe/PR-Beschreibung.
"""

import os
from decimal import Decimal

import ccxt.async_support as ccxt_async
import pytest

from app.betriebsart import Betriebsart
from app.schluessel import ExchangeCredential
from app.trading.papier import okx_demo

SYMBOL = "BTC/USDC"
# Weit unter dem Demo-Marktpreis (zuletzt ~83600 gemessen) -- löst sicher nicht aus.
NICHT_AUSLOESENDER_TRIGGER = Decimal("40000")


def _demo_credential(*, simulated_trading_header: bool = True) -> ExchangeCredential:
    api_key = os.environ.get("OKX_DEMO_API_KEY")
    api_secret = os.environ.get("OKX_DEMO_API_SECRET")
    passphrase = os.environ.get("OKX_DEMO_PASSPHRASE")
    if not api_key or not api_secret:
        pytest.skip("Kein OKX-Demo-Secret in der Umgebung -- Test übersprungen.")
    return ExchangeCredential(
        exchange="okx", api_key=api_key, api_secret=api_secret, passphrase=passphrase,
        betriebsart=Betriebsart.PAPIER, simulated_trading_header=simulated_trading_header,
    )


class _StopOrder:
    exchange = "okx"
    symbol = SYMBOL
    side = "sell"
    quantity = Decimal("0.001")
    order_type = "stop_market"
    limit_price = None
    stop_price = NICHT_AUSLOESENDER_TRIGGER


@pytest.mark.live
async def test_p4b_stop_market_platzieren_wiederfinden_stornieren():
    """P-4b, Abbruchkriterium: Stop-Market-Order platzieren, über
    ``lese_offene_algo_orders`` wiederfinden (``orders-algo-pending``), danach
    stornieren. Kein offener Rest bleibt zurück."""
    credential = _demo_credential()
    algo_id = await okx_demo.platziere_order(credential, _StopOrder())
    try:
        gefunden = await okx_demo.lese_offene_algo_orders(credential, SYMBOL)
        treffer = next((o for o in gefunden if o.algo_id == algo_id), None)
        assert treffer is not None, "eigene Stop-Order nicht in orders-algo-pending gefunden"
        assert treffer.side == "sell"
        assert treffer.menge == Decimal("0.001")
        assert treffer.trigger_kurs == NICHT_AUSLOESENDER_TRIGGER
    finally:
        storno_id = await okx_demo.storniere_algo_order(credential, algo_id, SYMBOL)
        assert storno_id == algo_id

    nach_storno = await okx_demo.lese_offene_algo_orders(credential, SYMBOL)
    assert all(o.algo_id != algo_id for o in nach_storno)


@pytest.mark.live
async def test_p4b_rohantwort_zeigt_stop_market_nicht_stop_limit():
    """P-4b, zweiter Teil: die rohe OKX-Antwort selbst belegt ``ordType=conditional``,
    ``slOrdPx=-1`` (Marktausführung, keine Stop-Limit-Order) und ``tdMode=cash``
    (Spot-Kassa-Modus, keine Cross-Margin-Order, Falle 2 aus Entwurf 8.3)."""
    credential = _demo_credential()
    client = _client_wie_okx_demo(credential)
    try:
        antwort = await client.create_order(
            SYMBOL, "market", "sell", 0.001, None,
            {"stopLossPrice": float(NICHT_AUSLOESENDER_TRIGGER), "tdMode": "cash"},
        )
        algo_id = antwort["id"]
        try:
            gefunden = await client.fetch_open_orders(SYMBOL, params={"ordType": "conditional"})
            info = next(o["info"] for o in gefunden if o["id"] == algo_id)
            assert info.get("ordType") == "conditional"
            assert info.get("slOrdPx") == "-1"
            assert info.get("tdMode") == "cash"
            assert Decimal(info.get("slTriggerPx")) == NICHT_AUSLOESENDER_TRIGGER
        finally:
            await client.cancel_order(algo_id, SYMBOL, params={"trigger": True})
    finally:
        await client.close()


@pytest.mark.live
async def test_p13_gegenprobe_slordpx_mit_explizitem_preis_ist_kein_minus_eins():
    """P-13: Nachweis, dass ``-1`` in P-4b kein Zufall ist, sondern an unserer
    Aufrufform hängt -- wird ``slOrdPx`` explizit mitgegeben, zeigt die Rohantwort den
    echten Limitpreis. **Nur im Test, nie im Produktionspfad** (okx_demo.platziere_order
    verbietet genau das strukturell, F-12 Ebene 1+2)."""
    credential = _demo_credential()
    client = _client_wie_okx_demo(credential)
    limit_preis = Decimal("39500")
    try:
        antwort = await client.create_order(
            SYMBOL, "market", "sell", 0.001, None,
            {
                "stopLossPrice": float(NICHT_AUSLOESENDER_TRIGGER),
                "tdMode": "cash",
                "slOrdPx": float(limit_preis),
            },
        )
        algo_id = antwort["id"]
        try:
            gefunden = await client.fetch_open_orders(SYMBOL, params={"ordType": "conditional"})
            info = next(o["info"] for o in gefunden if o["id"] == algo_id)
            assert Decimal(info.get("slOrdPx")) == limit_preis
        finally:
            await client.cancel_order(algo_id, SYMBOL, params={"trigger": True})
    finally:
        await client.close()


@pytest.mark.live
async def test_p15_stornierung_mit_falscher_algo_id_wirft_eigene_ausnahme():
    """P-15: eine nicht existierende ``algoId`` beim Stornieren ist eine eigene
    Ausnahme (``OkxDemoAblehnung``), kein stiller Ausweichweg."""
    credential = _demo_credential()
    with pytest.raises(okx_demo.OkxDemoFehler):
        await okx_demo.storniere_algo_order(credential, "nicht-existierende-algo-id", SYMBOL)


@pytest.mark.live
async def test_p15_ungueltiges_symbol_wirft_eigene_ausnahme():
    """P-15: ein Symbol, das OKX nicht kennt, ist eine eigene Ausnahme beim Lesen
    offener Algo-Order, kein stiller Ausweichweg (leere Liste wäre falsch -- das sähe
    wie "keine offenen Stops" aus, nicht wie ein Fehler)."""
    credential = _demo_credential()
    with pytest.raises(okx_demo.OkxDemoFehler):
        await okx_demo.lese_offene_algo_orders(credential, "NICHTEXISTENT/USDC")


@pytest.mark.live
async def test_p15_ohne_simulated_trading_header_scheitert_lesevorgang_an_umgebungsbindung():
    """P-15: wie beim Kontostand-Lesevorgang (HED-101, ``test_okx_demo_live.py``)
    lehnt OKX den Lesevorgang offener Algo-Order ohne den passenden
    simulated-trading-Header mit dem umgebungsbezogenen Fehlercode 50101 ab --
    sichtbar, nicht still."""
    credential = _demo_credential(simulated_trading_header=False)
    with pytest.raises(okx_demo.OkxDemoAblehnung) as exc_info:
        await okx_demo.lese_offene_algo_orders(credential, SYMBOL)
    assert "50101" in str(exc_info.value) or "environment" in str(exc_info.value).lower()


def _client_wie_okx_demo(credential: ExchangeCredential) -> ccxt_async.okx:
    """Gleiche Konstruktion wie ``okx_demo._client`` -- hier dupliziert, weil P-13
    bewusst einen Aufruf macht (``slOrdPx`` explizit), den das Produktionsmodul
    niemals zulässt."""
    client = ccxt_async.okx(
        {
            "apiKey": credential.api_key,
            "secret": credential.api_secret,
            "password": credential.passphrase,
            "enableRateLimit": True,
            "hostname": "eea.okx.com",
        }
    )
    client.set_sandbox_mode(True)
    return client
