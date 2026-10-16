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
            symbol="BTC/USDC",
            side="buy",
            quantity=Decimal("0.01"),
            order_type="market",
            limit_price=None,
            stop_price=None,
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
        fetch_order_result=None,
        fetch_order_exc=None,
        fetch_order_results=None,
        fetch_order_excs=None,
        fetch_open_orders_result=None,
        fetch_open_orders_exc=None,
        cancel_order_result=None,
        cancel_order_exc=None,
    ):
        self._create_order_result = create_order_result
        self._create_order_exc = create_order_exc
        self._account_balance_result = account_balance_result
        self._account_balance_exc = account_balance_exc
        self._fetch_order_result = fetch_order_result
        self._fetch_order_exc = fetch_order_exc
        # HED-147/HED-150: der zweistufige Algo-Order-Lookup ruft ``fetch_order``
        # zweimal mit unterschiedlichen Order-Ids auf -- diese beiden Listen liefern
        # je Aufruf (in Reihenfolge) eine eigene Antwort/Ausnahme, ohne das einfache
        # Verhalten von ``fetch_order_result``/``fetch_order_exc`` für die
        # bestehenden einstufigen Tests zu verändern.
        self._fetch_order_results = list(fetch_order_results or [])
        self._fetch_order_excs = list(fetch_order_excs or [])
        self._fetch_open_orders_result = fetch_open_orders_result
        self._fetch_open_orders_exc = fetch_open_orders_exc
        self._cancel_order_result = cancel_order_result
        self._cancel_order_exc = cancel_order_exc
        self.closed = False
        self.create_order_calls = []
        self.private_get_account_balance_calls = 0
        self.fetch_order_calls = []
        self.fetch_open_orders_calls = []
        self.cancel_order_calls = []

    async def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.create_order_calls.append((symbol, order_type, side, amount, price, params or {}))
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

    async def fetch_order(self, order_id, symbol, params=None):
        self.fetch_order_calls.append((order_id, symbol, params or {}))
        if self._fetch_order_excs:
            exc = self._fetch_order_excs.pop(0)
            if exc is not None:
                raise exc
        elif self._fetch_order_exc:
            raise self._fetch_order_exc
        if self._fetch_order_results:
            return self._fetch_order_results.pop(0)
        return self._fetch_order_result

    async def fetch_open_orders(self, symbol, params=None):
        self.fetch_open_orders_calls.append((symbol, params or {}))
        if self._fetch_open_orders_exc:
            raise self._fetch_open_orders_exc
        return self._fetch_open_orders_result

    async def cancel_order(self, order_id, symbol, params=None):
        self.cancel_order_calls.append((order_id, symbol, params or {}))
        if self._cancel_order_exc:
            raise self._cancel_order_exc
        return self._cancel_order_result

    async def close(self):
        self.closed = True


# --- _client: Konstruktion ohne Netzzugriff --------------------------------------


def test_client_lehnt_fremde_boerse_ohne_netzzugriff_ab():
    with pytest.raises(okx_demo.OkxDemoFehler):
        okx_demo._client(_credential(exchange="binance"))


def test_client_setzt_header_fuer_papier_betriebsart():
    client = okx_demo._client(_credential(simulated_trading_header=True))
    assert client.headers.get("x-simulated-trading") == "1"


def test_client_setzt_sandbox_modus_fuer_papier_betriebsart():
    """HED-92: ``set_sandbox_mode(True)`` statt der händischen Header-Zuweisung --
    setzt ``options['sandboxMode']``, das ccxt-/OKX-eigene Flag, das
    ``fetch_currencies()`` (Fehler 50038 im Demo-Modus) umgeht."""
    client = okx_demo._client(_credential(simulated_trading_header=True))
    assert client.options.get("sandboxMode") is True


def test_client_setzt_sandbox_modus_nicht_fuer_live_betriebsart():
    client = okx_demo._client(
        _credential(betriebsart=Betriebsart.LIVE, simulated_trading_header=False)
    )
    assert client.options.get("sandboxMode") is not True


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
    assert fake.create_order_calls == [("BTC/USDC", "market", "buy", 0.01, None, {})]
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


# --- B-12 (HED-84): Stop-Market-Platzierung, Prüfplan P-3/P-4a ----------------------


async def test_platziere_stop_market_order_erfolg(monkeypatch):
    fake = FakeOkxClient(create_order_result={"id": "algo-123"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    order_id = await okx_demo.platziere_order(
        _credential(),
        _Order(order_type="stop_market", side="sell", stop_price=Decimal("47500")),
    )

    assert order_id == "algo-123"
    assert fake.closed is True


async def test_platziere_stop_market_order_mit_limit_price_wird_strukturell_abgewiesen(
    monkeypatch,
):
    """Festlegung F-12, Ebene 3 (Falle 1, Entwurf Abschnitt 8.2): Abbruch VOR jedem
    Netzaufruf -- der Fake-Client darf nicht einmal konstruiert werden."""

    def kein_aufruf_erlaubt(_credential):
        raise AssertionError("_client darf bei Stop-Limit-Versuch nicht aufgerufen werden")

    monkeypatch.setattr(okx_demo, "_client", kein_aufruf_erlaubt)

    with pytest.raises(okx_demo.OkxDemoAblehnung):
        await okx_demo.platziere_order(
            _credential(),
            _Order(
                order_type="stop_market", side="sell",
                stop_price=Decimal("47500"), limit_price=Decimal("47400"),
            ),
        )


async def test_platziere_stop_market_order_anfragekoerper(monkeypatch):
    """P-4a: Anfragekörper-Prüfung am Fake-Client -- ``price=None`` (Literal, Falle 1),
    ``stopLossPrice``/``tdMode=cash`` in ``params`` (Falle 2), kein ``px``/``slOrdPx``
    in den übergebenen Zusatzparametern."""
    fake = FakeOkxClient(create_order_result={"id": "algo-123"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    await okx_demo.platziere_order(
        _credential(),
        _Order(order_type="stop_market", side="sell", stop_price=Decimal("47500")),
    )

    [(symbol, order_type, side, amount, price, params)] = fake.create_order_calls
    assert (symbol, order_type, side, amount) == ("BTC/USDC", "market", "sell", 0.01)
    assert price is None
    assert params == {"stopLossPrice": 47500.0, "tdMode": "cash"}
    assert "slOrdPx" not in params
    assert "px" not in params


async def test_stop_market_order_timeout_wird_nicht_verschluckt(monkeypatch):
    fake = FakeOkxClient(create_order_exc=ccxt.RequestTimeout("kein Pong"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoTimeout):
        await okx_demo.platziere_order(
            _credential(),
            _Order(order_type="stop_market", side="sell", stop_price=Decimal("47500")),
        )
    assert fake.closed is True


async def test_stop_market_order_ablehnung_durch_boerse(monkeypatch):
    fake = FakeOkxClient(create_order_exc=ccxt.InvalidOrder("tdMode error"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAblehnung):
        await okx_demo.platziere_order(
            _credential(),
            _Order(order_type="stop_market", side="sell", stop_price=Decimal("47500")),
        )
    assert fake.closed is True


# --- lese_order_status ---------------------------------------------------------------


async def test_lese_order_status_vollstaendig_gefuellt(monkeypatch):
    fake = FakeOkxClient(
        fetch_order_result={
            "status": "closed", "filled": "0.5", "remaining": "0",
            "lastUpdateTimestamp": 1700000000000,
        }
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.status == "gefuellt"
    assert ergebnis.gefuellte_menge == Decimal("0.5")
    assert fake.fetch_order_calls == [("order-1", "BTC/USDC", {})]
    assert fake.closed is True


async def test_lese_order_status_teilgefuellt_noch_offen(monkeypatch):
    fake = FakeOkxClient(fetch_order_result={"status": "open", "filled": "0.3", "remaining": "0.2"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.status == "teilgefuellt"
    assert ergebnis.gefuellte_menge == Decimal("0.3")
    assert ergebnis.restmenge == Decimal("0.2")


async def test_lese_order_status_offen_ohne_fuellung(monkeypatch):
    fake = FakeOkxClient(fetch_order_result={"status": "open", "filled": "0", "remaining": "0.5"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.status == "offen"


async def test_lese_order_status_beendet_mit_teilfuellung(monkeypatch):
    fake = FakeOkxClient(
        fetch_order_result={"status": "canceled", "filled": "0.2", "remaining": "0"}
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.status == "beendet"
    assert ergebnis.gefuellte_menge == Decimal("0.2")


async def test_lese_order_status_abgelehnt(monkeypatch):
    fake = FakeOkxClient(fetch_order_result={"status": "rejected", "filled": "0"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.status == "abgelehnt"


async def test_lese_order_status_timeout_wirft(monkeypatch):
    fake = FakeOkxClient(fetch_order_exc=ccxt.RequestTimeout("kein Pong"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoTimeout):
        await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")


# --- HED-147: Preisfeld in lese_order_status (Teilaufgabe B, normale Order) --------


async def test_lese_order_status_liefert_durchschnittspreis_wenn_vorhanden(monkeypatch):
    """Nachtrag-Handprobe HED-147 Abschnitt 2 Punkt 4: für eine gewöhnliche
    (nicht-Algo) Order liefert ccxt ``average`` bereits in der ersten Antwort."""
    fake = FakeOkxClient(
        fetch_order_result={
            "status": "closed", "filled": "0.01", "remaining": "0", "average": "81641.0",
        }
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.durchschnittspreis == Decimal("81641.0")


async def test_lese_order_status_ohne_preisfeld_ist_durchschnittspreis_none(monkeypatch):
    fake = FakeOkxClient(fetch_order_result={"status": "open", "filled": "0", "remaining": "1"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.durchschnittspreis is None


async def test_lese_order_status_mit_nicht_positivem_preis_ist_durchschnittspreis_none(
    monkeypatch,
):
    fake = FakeOkxClient(
        fetch_order_result={"status": "closed", "filled": "0.01", "remaining": "0", "average": "0"}
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")

    assert ergebnis.durchschnittspreis is None


# --- HED-147/HED-150: lese_algo_order_status (Teilaufgabe A, Stop-Order, Weg 3) ----


_ALGO_GEFUELLT_ANTWORT = {
    "status": "closed",
    "filled": "0.001",
    "remaining": "0",
    "lastUpdateTimestamp": 1700000000000,
    "info": {"state": "effective", "ordId": "normal-order-1", "ordIdList": ["normal-order-1"]},
}
_FOLGE_ANTWORT_MIT_PREIS = {
    "status": "closed", "filled": "0.001", "average": "81641.0",
}


async def test_lese_algo_order_status_erfolg_zweistufiger_lookup(monkeypatch):
    fake = FakeOkxClient(
        fetch_order_results=[_ALGO_GEFUELLT_ANTWORT, _FOLGE_ANTWORT_MIT_PREIS]
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_algo_order_status(_credential(), "algo-1", "BTC/USDC")

    assert ergebnis.status == "gefuellt"
    assert ergebnis.gefuellte_menge == Decimal("0.001")
    assert ergebnis.durchschnittspreis == Decimal("81641.0")
    assert fake.fetch_order_calls == [
        ("algo-1", "BTC/USDC", {"trigger": True}),
        ("normal-order-1", "BTC/USDC", {}),
    ]
    assert fake.closed is True


async def test_lese_algo_order_status_ohne_fuellung_fragt_nicht_zweites_mal_ab(monkeypatch):
    fake = FakeOkxClient(
        fetch_order_result={"status": "open", "filled": "0", "remaining": "0.001"}
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_algo_order_status(_credential(), "algo-1", "BTC/USDC")

    assert ergebnis.status == "offen"
    assert ergebnis.durchschnittspreis is None
    assert len(fake.fetch_order_calls) == 1


async def test_lese_algo_order_status_erste_abfrage_timeout_wirft(monkeypatch):
    fake = FakeOkxClient(fetch_order_exc=ccxt.RequestTimeout("kein Pong"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoTimeout):
        await okx_demo.lese_algo_order_status(_credential(), "algo-1", "BTC/USDC")
    assert fake.closed is True


@pytest.mark.parametrize(
    "ord_id_list",
    [[], ["a", "b"], None, "nicht-eine-liste"],
)
async def test_lese_algo_order_status_ordidlist_nicht_genau_ein_eintrag_ist_fail_closed(
    monkeypatch, ord_id_list
):
    """HED-150 Auflage 4: fail-closed für JEDEN Wert ungleich genau einem Eintrag."""
    antwort = dict(_ALGO_GEFUELLT_ANTWORT)
    antwort["info"] = {"ordIdList": ord_id_list}
    fake = FakeOkxClient(fetch_order_results=[antwort])
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAntwortFehler):
        await okx_demo.lese_algo_order_status(_credential(), "algo-1", "BTC/USDC")
    assert fake.closed is True


async def test_lese_algo_order_status_zweite_abfrage_ohne_preis_ist_fail_closed(monkeypatch):
    fake = FakeOkxClient(
        fetch_order_results=[_ALGO_GEFUELLT_ANTWORT, {"status": "closed", "filled": "0.001"}]
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAntwortFehler):
        await okx_demo.lese_algo_order_status(_credential(), "algo-1", "BTC/USDC")


async def test_lese_algo_order_status_zweite_abfrage_timeout_wirft(monkeypatch):
    fake = FakeOkxClient(
        fetch_order_results=[_ALGO_GEFUELLT_ANTWORT],
        fetch_order_excs=[None, ccxt.RequestTimeout("kein Pong")],
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoTimeout):
        await okx_demo.lese_algo_order_status(_credential(), "algo-1", "BTC/USDC")
    assert fake.closed is True


async def test_lese_algo_order_status_teilgefuellt_fragt_preis_trotzdem_ab(monkeypatch):
    """gefuellte_menge > 0 ist das Kriterium für den zweiten Lookup, nicht der
    Endzustand -- auch eine (noch offene) Teilfüllung hat bereits einen Fill-Preis."""
    antwort = {
        "status": "open",
        "filled": "0.0005",
        "remaining": "0.0005",
        "info": {"ordIdList": ["normal-order-1"]},
    }
    fake = FakeOkxClient(fetch_order_results=[antwort, _FOLGE_ANTWORT_MIT_PREIS])
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnis = await okx_demo.lese_algo_order_status(_credential(), "algo-1", "BTC/USDC")

    assert ergebnis.status == "teilgefuellt"
    assert ergebnis.durchschnittspreis == Decimal("81641.0")


async def test_lese_order_status_unbekannter_status_wirft_antwortfehler(monkeypatch):
    fake = FakeOkxClient(fetch_order_result={"status": "irgendwas", "filled": "0"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAntwortFehler):
        await okx_demo.lese_order_status(_credential(), "order-1", "BTC/USDC")


# --- lese_offene_algo_orders ---------------------------------------------------------


async def test_lese_offene_algo_orders_erfolg(monkeypatch):
    fake = FakeOkxClient(
        fetch_open_orders_result=[
            {
                "id": "algo-1", "side": "sell", "amount": "0.5",
                "stopLossPrice": "47500", "lastUpdateTimestamp": 1700000000000,
            }
        ]
    )
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnisse = await okx_demo.lese_offene_algo_orders(_credential(), "BTC/USDC")

    assert len(ergebnisse) == 1
    assert ergebnisse[0].algo_id == "algo-1"
    assert ergebnisse[0].menge == Decimal("0.5")
    assert ergebnisse[0].trigger_kurs == Decimal("47500")
    assert fake.fetch_open_orders_calls == [("BTC/USDC", {"ordType": "conditional"})]


async def test_lese_offene_algo_orders_leer(monkeypatch):
    fake = FakeOkxClient(fetch_open_orders_result=[])
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    ergebnisse = await okx_demo.lese_offene_algo_orders(_credential(), "BTC/USDC")

    assert ergebnisse == []


async def test_lese_offene_algo_orders_unerwartetes_format_wirft(monkeypatch):
    fake = FakeOkxClient(fetch_open_orders_result=[{"id": "algo-1"}])  # ohne Pflichtfelder
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAntwortFehler):
        await okx_demo.lese_offene_algo_orders(_credential(), "BTC/USDC")


async def test_lese_offene_algo_orders_timeout_wirft(monkeypatch):
    fake = FakeOkxClient(fetch_open_orders_exc=ccxt.RequestTimeout("kein Pong"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoTimeout):
        await okx_demo.lese_offene_algo_orders(_credential(), "BTC/USDC")


# --- storniere_algo_order ------------------------------------------------------------


async def test_storniere_algo_order_erfolg(monkeypatch):
    fake = FakeOkxClient(cancel_order_result={"id": "algo-1"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    storno_id = await okx_demo.storniere_algo_order(_credential(), "algo-1", "BTC/USDC")

    assert storno_id == "algo-1"
    assert fake.cancel_order_calls == [("algo-1", "BTC/USDC", {"trigger": True})]
    assert fake.closed is True


async def test_storniere_algo_order_ablehnung(monkeypatch):
    fake = FakeOkxClient(cancel_order_exc=ccxt.InvalidOrder("unbekannte algoId"))
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAblehnung):
        await okx_demo.storniere_algo_order(_credential(), "algo-1", "BTC/USDC")


async def test_storniere_algo_order_ohne_id_wirft_antwortfehler(monkeypatch):
    fake = FakeOkxClient(cancel_order_result={"status": "ok"})
    monkeypatch.setattr(okx_demo, "_client", lambda credential: fake)

    with pytest.raises(okx_demo.OkxDemoAntwortFehler):
        await okx_demo.storniere_algo_order(_credential(), "algo-1", "BTC/USDC")
