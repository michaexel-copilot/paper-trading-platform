"""Echter Netzzugriff gegen OKX-Demo -- Beweis für HED-55 Selbsttest-Punkt 4.

Anders als test_okx_demo.py (Fake-Client, kein Netzzugriff) spricht diese Datei den
echten OKX-Demo-Endpunkt an. Per ``pyproject.toml`` (``addopts = "-m 'not live' -q"``)
läuft sie nicht in der normalen Testrunde, sondern nur explizit (``pytest -m live``) --
genau wie die bestehenden ``live``-Tests in test_marketdata.py. Setup: OKX_DEMO_API_KEY/
_API_SECRET/_PASSPHRASE müssen als Secret vorliegen (siehe HED-76); ohne das Secret wird
der Test übersprungen, nicht als Fehlschlag gewertet.

CRO-Nachforderung (HED-101, 14.10.2026) zu PR #24: der Vergleich in
``verifiziere_kontostand`` (``ergebnis.betriebsart == credential.betriebsart.value``)
war tautologisch -- ``ergebnis.betriebsart`` wird in ``okx_demo.lese_kontostand`` wörtlich
aus demselben ``credential.betriebsart.value`` gesetzt und kann nie widersprechen. Die
tatsächliche Schutzwirkung, die Punkt 4 ("Ergebnis muss zur Betriebsart passen") braucht,
kommt nicht aus unserem eigenen Vergleich, sondern von OKX selbst: ein Schlüssel ist bei
OKX an die Umgebung gebunden, in der er angelegt wurde, und ein Header, der nicht zu
dieser Umgebung passt, wird mit einem eigenen Fehlercode abgelehnt. Dieser Test beweist
das reproduzierbar, statt es nur zu behaupten.
"""

import os

import pytest

from app.betriebsart import Betriebsart
from app.schluessel import ExchangeCredential
from app.trading.papier.okx_demo import OkxDemoAblehnung, lese_kontostand


def _demo_credential(*, simulated_trading_header: bool) -> ExchangeCredential:
    api_key = os.environ.get("OKX_DEMO_API_KEY")
    api_secret = os.environ.get("OKX_DEMO_API_SECRET")
    passphrase = os.environ.get("OKX_DEMO_PASSPHRASE")
    if not api_key or not api_secret:
        pytest.skip("Kein OKX-Demo-Secret in der Umgebung -- Test übersprungen.")
    return ExchangeCredential(
        exchange="okx",
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        betriebsart=Betriebsart.PAPIER,
        simulated_trading_header=simulated_trading_header,
    )


@pytest.mark.live
async def test_live_demo_schluessel_mit_richtigem_header_liest_kontostand():
    """Gegenprobe zum Ablehnungstest unten: der Demo-Schlüssel MIT dem
    simulated-trading-Header funktioniert -- sonst wäre der Ablehnungstest kein Beweis
    für eine Umgebungsbindung, sondern nur für einen grundsätzlich kaputten Schlüssel."""
    credential = _demo_credential(simulated_trading_header=True)
    ergebnis = await lese_kontostand(credential)
    assert ergebnis.quelle == "okx_demo"
    assert ergebnis.betriebsart == "papier"


@pytest.mark.live
async def test_live_demo_schluessel_ohne_header_wird_von_okx_wegen_umgebung_abgelehnt():
    """Der Risikofall aus der CRO-Nachforderung: ein unter OKX_DEMO_* abgelegter
    Schlüssel wird ohne den zugehörigen simulated-trading-Header angesprochen -- so,
    wie es passieren würde, wenn der Schlüssel tatsächlich nur für eine andere
    Umgebung gültig wäre. OKX lehnt das mit einem eigenen, auf die Umgebungsbindung
    bezogenen Fehlercode ab (gemessen 2026-10-14 gegen eea.okx.com:
    ``AuthenticationError: okx {"msg":"APIKey does not match current environment.",
    "code":"50101"}``) -- das ist das Gegenstellen-Signal, auf das sich Punkt 4
    tatsächlich stützt, nicht der Feldvergleich in ``verifiziere_kontostand``.

    Ein Live-Schlüssel wird hier bewusst nicht getestet (Rollenregel: der Trading
    Systems Engineer hält nie einen Live-Schlüssel). Die Umgebungsbindung ist laut
    OKX-Fehlercode 50101 symmetrisch zur Schlüssel-Registrierung, nicht zur Richtung
    des Headers -- dieser Test belegt eine Richtung, die andere folgt aus demselben
    Mechanismus.
    """
    credential = _demo_credential(simulated_trading_header=False)
    with pytest.raises(OkxDemoAblehnung) as exc_info:
        await lese_kontostand(credential)
    assert "50101" in str(exc_info.value) or "environment" in str(exc_info.value).lower()
