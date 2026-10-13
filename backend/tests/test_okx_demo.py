"""B-6 (HED-76): echter OKX-Demo-Orderpfad und Kontostand-Lesevorgang.

Kein Netzwerkzugriff in diesen Tests -- ``okx_demo._client`` wird durch einen
Fake-Client ersetzt, genau wie ``RecordedExchange`` in test_marketdata.py für den
Kursquellen-Adapter. OKX-Demo selbst wird hier nicht angesprochen; das ist Sache der
manuellen technischen Abnahme gegen den echten Demo-Endpunkt, nicht dieser Testdatei.
"""

from decimal import Decimal
from types import SimpleNamespace

import ccxt
import pytest

from app.betriebsart import Betriebsart
from app.schluessel import ExchangeCredential
from app.trading.papier import okx_demo


def _credential(**overrides) -> ExchangeCredential:
    felder = dict(
        exchange="okx",
        api_key="demo-key",
        api_secret="demo-secret",
        passphrase="demo-passphrase",
        betriebsart=Betriebsart.PAPIER,
        simulated_trading_header=True,
    )
    felder.update(overrides)
    return ExchangeCredential(**felder)


class _Order:
    def __init__(self, **overrides):
        felder = dict(
            exchange="okx",
            symbol="BTC/USDT",
            side="buy",
            quantity=Decimal("0.01"),
            order_type="market",
            limit_price=None,
        )
        felder.update(overrides)
        for name, wert in felder.items():
            setattr(self, name, wert)


class FakeOkxClient:
    def __init__(
        self,
        create_order_result=None,
        create_order_exc=None,
        account_balance_result=None,
        account_balance_exc=None,
    ):
        self._create_order_result = create_order_result
        self._create_order_exc = create_order_exc
        self._account_balance_result = account_balance_result
        self._account_balance_exc = account_balance_exc
        self.closed = False
        self.create_order_calls = []
        self.private_get_account_balance_calls = 0

    async def create_order(self, symbol, order_type, side, amount, price=None):
        self.create_order_calls.append((symbol, order_type, side, amount, price))
        if self._create_order_exc:
            raise self._create_order_exc
        return self._create_order_result

    async def privateGetAccountBalance(self):
        """HED-90: ersetzt ``fetch_balance`` -- das Produktivmodul ruft diese
        implizite ccxt-Methode direkt auf (``GET /api/v5/account/balance``, ohne
        den von ``fetch_balance()`` ausgelösten ``load_markets``-Zwischenaufruf,
        der im Demo-Modus mit Fehler 50038 scheitert)."""
        self.private_get_account_balance_calls += 1
        if self._account_balance_exc:
            raise self._account_balance_exc
        return self._account_balance_result

    async def close(self):
        self.closed = True


# --- _client: Konstruktion ohne Netzzugriff --------------------------------------


def test_client_lehnt_fremde_boerse_ohne_netzzugriff_ab():
    with pytest.raises(okx_demo.OkxDemoFehler):
        okx_demo._client(_credential(exchange="binance"))


def test_client_setzt_header_fuer_papier_betriebsart():
    client = okx_demo._client(_credential(simulated_trading_header=True))
    assert client.headers.get("x-simulated-trading") == "1"


def test_client_setzt_header_nicht_fuer_live_betriebsart():
    client = okx_demo._client(
        _credential(betriebsart=Betriebsart.LIVE, simulated_trading_header=False)
    )
    assert "x-simulated-trading" not in (client.headers or {})


def test_client_verwendet_eea_hostname_als_standard():
    """HED-87: der OKX-Demo-Schlüssel des Boards existiert nur auf eea.okx.com
    (OKX-EWR-Einheit), nicht auf www.okx.com/app.okx.com (Fehlercode 50119,
    Handtest Board 2026-10-13). Standard ohne jede Konfiguration."""
    client = okx_demo._client(_credential())
    assert client.hostname == "eea.okx.com"


def test_client_hostname_kommt_aus_der_konfiguration_nicht_fest_verdrahtet(monkeypatch):
    einstellungen = SimpleNamespace(okx_demo_hostname="anderer.okx.beispiel")
    monkeypatch.setattr(okx_demo, "get_settings", lambda: einstellungen)

    client = okx_demo._client(_credential())

    assert client.hostname == "anderer.okx.beispiel"


# --- platziere_order ---------------------------------------------------------------


async def test_platziere_order_erfolg(monkeypatch):
    fake = FakeOkxClient(create_order_result={"id": "okx-demo-123"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    order_id = await okx_demo.platziere_order(_credential(), _Order())

    assert order_id == "okx-demo-123"
    assert fake.create_order_calls == [("BTC/USDT", "market", "buy", 0.01, None)]
    assert fake.closed is True


async def test_platziere_order_timeout_wird_nicht_verschluckt(monkeypatch):
    fake = FakeOkxClient(create_order_exc=ccxt.RequestTimeout("kein Pong"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoTimeout):
        await okx_demo.platziere_order(_credential(), _Order())
    assert fake.closed is True


async def test_platziere_order_ablehnung_durch_boerse(monkeypatch):
    fake = FakeOkxClient(create_order_exc=ccxt.InvalidOrder("Betrag zu klein"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAblehnung):
        await okx_demo.platziere_order(_credential(), _Order())
    assert fake.closed is True


async def test_platziere_order_antwort_ohne_order_id(monkeypatch):
    fake = FakeOkxClient(create_order_result={"status": "ok"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAntwortFehler):
        await okx_demo.platziere_order(_credential(), _Order())
    assert fake.closed is True


async def test_platziere_order_unbekannter_ordertyp_ruft_client_nicht_auf(monkeypatch):
    def kein_aufruf_erlaubt(_credential):
        raise AssertionError("_client darf bei unbekanntem Ordertyp nicht aufgerufen werden")

    monkeypatch.setattr(okx_demo, "_client", kein_aufruf_erlaubt)

    with pytest.raises(okx_demo.OkxDemoAblehnung):
        await okx_demo.platziere_order(_credential(), _Order(order_type="stop"))


# --- lese_kontostand ---------------------------------------------------------------


async def test_lese_kontostand_erfolg(monkeypatch):
    """HED-90: rohdaten ist jetzt die rohe OKX-Antwort von GET /api/v5/account/balance
    (code/data/msg), nicht mehr ccxt's vereinheitlichte Bilanzstruktur."""
    rohdaten = {
        "code": "0",
        "msg": "",
        "data": [{"totalEq": "477500", "details": [{"ccy": "USDT", "cashBal": "1000"}]}],
    }
    fake = FakeOkxClient(account_balance_result=rohdaten)
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_kontostand(_credential())

    assert ergebnis.quelle == "okx_demo"
    assert ergebnis.betriebsart == "papier"
    assert ergebnis.rohdaten == rohdaten
    assert fake.private_get_account_balance_calls == 1
    assert fake.closed is True


async def test_lese_kontostand_timeout(monkeypatch):
    fake = FakeOkxClient(account_balance_exc=ccxt.RequestTimeout("kein Pong"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoTimeout):
        await okx_demo.lese_kontostand(_credential())
    assert fake.closed is True


async def test_lese_kontostand_ablehnung(monkeypatch):
    fake = FakeOkxClient(account_balance_exc=ccxt.AuthenticationError("falscher Schlüssel"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAblehnung):
        await okx_demo.lese_kontostand(_credential())
    assert fake.closed is True


async def test_lese_kontostand_ablehnung_bei_gesperrtem_endpunkt_im_demo_modus(monkeypatch):
    """HED-90: Regressionstest für den ursprünglichen Fehler -- ein Endpunkt, den OKX
    im Demo-Modus sperrt, muss weiterhin als OkxDemoAblehnung ankommen, nicht als
    unbehandelte ccxt-Ausnahme."""
    fake = FakeOkxClient(
        account_balance_exc=ccxt.ExchangeError(
            'okx {"code":"50038","msg":"This feature is unavailable in demo trading"}'
        )
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAblehnung):
        await okx_demo.lese_kontostand(_credential())
    assert fake.closed is True


@pytest.mark.parametrize(
    "rohdaten",
    ["nicht-ein-dict", {}, {"code": "0", "msg": "", "data": []}, {"code": "0", "msg": ""}],
)
async def test_lese_kontostand_falsches_format(monkeypatch, rohdaten):
    fake = FakeOkxClient(account_balance_result=rohdaten)
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAntwortFehler):
        await okx_demo.lese_kontostand(_credential())
    assert fake.closed is True
