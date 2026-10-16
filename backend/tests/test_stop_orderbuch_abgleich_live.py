"""Echter Netzzugriff gegen OKX-Demo für den täglichen Stop-Orderbuch-Abgleich (B-12,
HED-84, Entwurf Abschnitt 10, Prüfplan Abschnitt 13.2).

Wie ``test_okx_demo_stop_live.py``: läuft nicht in der normalen Testrunde, nur explizit
über ``pytest -m live``. Ohne OKX-Demo-Secret wird übersprungen, nicht als Fehlschlag
gewertet. Keine dieser Messungen ist eine Performance- oder Renditemessung (Handbuch
Abschnitt 7) -- es geht ausschließlich um die technische Abnahme des Abgleichpfads.

Anders als ``test_stop_orderbuch_abgleich.py`` (P-7/P-8, Fake-Gegenstelle) spricht
``_LiveEngpass`` hier den echten OKX-Demo-Lesepfad an (``lese_offene_algo_orders``);
die Datenbankseite bleibt die normale Test-SQLite aus ``conftest.py`` -- nur der
Gegenstellen-Blick ist real, genau wie bei ``test_okx_demo_stop_live.py``.

**Abbruchkriterium P-14** (Entwurf 13.2): schlägt dieser Test fehl, erkennt der
Abgleich einen real erzeugten Unterschied zwischen eigenem Buch und Börse nicht --
dann dem CRO einen geänderten Entwurf vorlegen, nicht improvisieren.
"""

import os
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.betriebsart import Betriebsart
from app.dbtypes import utcnow
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.models.stop_order import StopAbgleichLauf, StopOrderBuch
from app.schluessel import ExchangeCredential
from app.sperrkennzeichen import ist_firmenweit_gesperrt
from app.trading.papier import okx_demo
from app.trading.stop_orderbuch_abgleich import pruefe_einmal
from app.trading.versandengpass import Versandengpass

SYMBOL = "BTC/USDC"
NICHT_AUSLOESENDER_TRIGGER = Decimal("40000")
TOLERANZ = Decimal("0.001")
TOTMANNFRIST = timedelta(hours=26)


def _demo_credential() -> ExchangeCredential:
    api_key = os.environ.get("OKX_DEMO_API_KEY")
    api_secret = os.environ.get("OKX_DEMO_API_SECRET")
    passphrase = os.environ.get("OKX_DEMO_PASSPHRASE")
    if not api_key or not api_secret:
        pytest.skip("Kein OKX-Demo-Secret in der Umgebung -- Test übersprungen.")
    return ExchangeCredential(
        exchange="okx", api_key=api_key, api_secret=api_secret, passphrase=passphrase,
        betriebsart=Betriebsart.PAPIER, simulated_trading_header=True,
    )


class _StopOrder:
    exchange = "okx"
    symbol = SYMBOL
    side = "sell"
    order_type = "stop_market"
    limit_price = None

    def __init__(self, quantity: Decimal, stop_price: Decimal = NICHT_AUSLOESENDER_TRIGGER):
        self.quantity = quantity
        self.stop_price = stop_price


class _LiveEngpass(Versandengpass):
    """Wie ``_FakeEngpass`` in ``test_stop_orderbuch_abgleich.py``, aber mit echtem
    Netzzugriff -- derselbe Überschreibungspunkt, damit der restliche Abgleichpfad
    (Festlegung F-1: nur diese Methode spricht die Gegenstelle für Lesevorgänge an)
    unverändert bleibt."""

    def __init__(self, credential: ExchangeCredential):
        super().__init__()
        self._credential = credential

    async def lese_offene_stop_orders(self, exchange, symbol):
        return await okx_demo.lese_offene_algo_orders(self._credential, symbol)


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


@pytest.fixture(autouse=True)
def papier_betriebsart(betriebsart):
    betriebsart("papier")


async def _eigene_zeile(session, **overrides) -> StopOrderBuch:
    felder = dict(
        strategie_schluessel="1", exchange="okx", symbol=SYMBOL, side="sell",
        menge_soll=Decimal("0.001"), menge_bestaetigt=None,
        trigger_soll=NICHT_AUSLOESENDER_TRIGGER, trigger_bestaetigt=None,
        gegenstellen_algo_id=None, zustand="offen",
    )
    felder.update(overrides)
    zeile = StopOrderBuch(**felder)
    session.add(zeile)
    await session.flush()
    return zeile


async def _ausloesergesetzt(session, ausloeser: str) -> int:
    zeilen = list(
        await session.scalars(
            select(SperrkennzeichenProtokoll).where(
                SperrkennzeichenProtokoll.ausloeser == ausloeser
            )
        )
    )
    return len(zeilen)


@pytest.mark.live
async def test_p14a_an_boerse_stornierter_stop_wird_als_fehlend_erkannt(session, session_factory):
    """P-14(a): Stop real platzieren, eigene Buchzeile dafür anlegen, Stop dann
    "manuell" an der Börse stornieren (ohne die eigene Zeile anzufassen) -- der
    Abgleich muss das als "fehlend" erkennen und das firmenweite Kennzeichen setzen."""
    credential = _demo_credential()
    algo_id = await okx_demo.platziere_order(credential, _StopOrder(Decimal("0.001")))
    storniert = False
    try:
        await _eigene_zeile(session, gegenstellen_algo_id=algo_id)
        await session.commit()

        await okx_demo.storniere_algo_order(credential, algo_id, SYMBOL)
        storniert = True

        engpass = _LiveEngpass(credential)
        bestanden = await pruefe_einmal(
            session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
        )

        assert bestanden is False
        assert await ist_firmenweit_gesperrt(session) is True
        lauf = await session.scalar(select(StopAbgleichLauf))
        assert lauf.ergebnis == "abweichung"
        assert lauf.anzahl_fehlend == 1
        assert await _ausloesergesetzt(session, "stop_orderbuch_abweichung") == 1
    finally:
        if not storniert:
            await okx_demo.storniere_algo_order(credential, algo_id, SYMBOL)


@pytest.mark.live
async def test_p14b_stop_ohne_eigene_buchzeile_wird_als_waise_erkannt(session, session_factory):
    """P-14(b): Stop real platzieren, aber KEINE eigene Buchzeile dafür anlegen (nur
    eine ältere, stornierte Zeile zum selben Symbol, damit der 30-Tage-Rückblick --
    Entwurf 10.4 -- das Symbol überhaupt abfragt) -- der Abgleich muss das als "Waise"
    erkennen."""
    credential = _demo_credential()
    algo_id = await okx_demo.platziere_order(credential, _StopOrder(Decimal("0.001")))
    try:
        await _eigene_zeile(
            session, zustand="storniert", gegenstellen_algo_id=f"{algo_id}-alt",
            beendet_at=utcnow(),
        )
        await session.commit()

        engpass = _LiveEngpass(credential)
        bestanden = await pruefe_einmal(
            session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
        )

        assert bestanden is False
        lauf = await session.scalar(select(StopAbgleichLauf))
        assert lauf.ergebnis == "abweichung"
        assert lauf.anzahl_waisen == 1
        assert await _ausloesergesetzt(session, "stop_orderbuch_abweichung") == 1
    finally:
        await okx_demo.storniere_algo_order(credential, algo_id, SYMBOL)


@pytest.mark.live
async def test_p14c_abweichende_menge_wird_als_mengenabweichung_erkannt(session, session_factory):
    """P-14(c): Stop real mit Menge 0,001 platzieren, eigene Buchzeile mit deutlich
    abweichender bestätigter Menge anlegen -- der Abgleich muss das als
    "Mengenabweichung" erkennen (weit über der 0,1%-Toleranz, E-8)."""
    credential = _demo_credential()
    algo_id = await okx_demo.platziere_order(credential, _StopOrder(Decimal("0.001")))
    try:
        await _eigene_zeile(
            session, gegenstellen_algo_id=algo_id, menge_soll=Decimal("0.5"),
            menge_bestaetigt=Decimal("0.5"),
        )
        await session.commit()

        engpass = _LiveEngpass(credential)
        bestanden = await pruefe_einmal(
            session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
        )

        assert bestanden is False
        lauf = await session.scalar(select(StopAbgleichLauf))
        assert lauf.ergebnis == "abweichung"
        assert lauf.anzahl_mengenabweichung == 1
        assert await _ausloesergesetzt(session, "stop_orderbuch_abweichung") == 1
    finally:
        await okx_demo.storniere_algo_order(credential, algo_id, SYMBOL)


@pytest.mark.live
async def test_p14_uebereinstimmung_bleibt_bestanden(session, session_factory):
    """Gegenprobe zu P-14: platziert man Stop und eigene Zeile deckungsgleich (Menge
    erst nach dem ersten Abgleich bestätigt, wie im echten Betrieb über F-15), bleibt
    der Abgleich bestanden -- sonst wären die drei Abweichungstests oben nicht
    aussagekräftig, sondern nur ein grundsätzlich kaputter Vergleich."""
    credential = _demo_credential()
    algo_id = await okx_demo.platziere_order(credential, _StopOrder(Decimal("0.001")))
    try:
        await _eigene_zeile(session, gegenstellen_algo_id=algo_id)
        await session.commit()

        engpass = _LiveEngpass(credential)
        bestanden = await pruefe_einmal(
            session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
        )

        assert bestanden is True
        assert await ist_firmenweit_gesperrt(session) is False
    finally:
        await okx_demo.storniere_algo_order(credential, algo_id, SYMBOL)
