"""Pfad 7 (HED-185, CRO-Entscheidung HED-191 Abschnitt 2): Stufe-1- (24h) und
Stufe-2-Fristwächter (4h) aus HED-8 §6.1/§6.3. Nur Eskalation/Protokoll, keine
automatische Order -- siehe Moduldokumentation von ``app.trading.fristwaechter``.

Backdating der Fristen folgt demselben Muster wie
``tests/test_proposals.py::test_fristwaechter_verfallt_ueberfaelligen_vorschlag_mit_system_frist``:
den gespeicherten Zeitpunkt direkt über die ``session``-Fixture in die Vergangenheit
setzen, dann den Fristwächter über die separate ``session_factory``-Fixture laufen
lassen (beide zeigen in den Tests auf dieselbe Datei, siehe ``tests/conftest.py``)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.dbtypes import utcnow
from app.kill_switch import hebe_strategie_auf, setze_firmenweit, setze_strategie
from app.models import Position, Strategy
from app.models.fristwaechter import FristEskalation
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.trading.fristwaechter import (
    ausloese_zeitpunkt,
    pruefe_stufe1_fristen,
    pruefe_stufe2_fristen,
)
from tests.conftest import D, asset_id, create_portfolio


@pytest.fixture(autouse=True)
def papier_betriebsart(betriebsart):
    betriebsart("papier")


async def _letzte_protokollzeile(session, *, kennzeichen_typ, strategie_schluessel=None):
    return await session.scalar(
        select(SperrkennzeichenProtokoll)
        .where(
            SperrkennzeichenProtokoll.kennzeichen_typ == kennzeichen_typ,
            SperrkennzeichenProtokoll.strategie_schluessel == strategie_schluessel,
        )
        .order_by(SperrkennzeichenProtokoll.id.desc())
        .limit(1)
    )


async def _backdatiere(session, zeile, delta: timedelta) -> None:
    zeile.occurred_at = utcnow() - delta
    await session.commit()


async def _eskalationen(session):
    return list(await session.scalars(select(FristEskalation)))


# --- ausloese_zeitpunkt: Herkunft des Fristbeginns --------------------------------


async def test_ausloese_zeitpunkt_none_wenn_nie_gesetzt(session):
    zeitpunkt = await ausloese_zeitpunkt(session, kennzeichen_typ="firmenweit")
    assert zeitpunkt is None


async def test_ausloese_zeitpunkt_ist_occurred_at_der_gesetzt_zeile(session):
    await setze_firmenweit(session, ausloeser="stufe2", begruendung="Test")
    zeile = await _letzte_protokollzeile(session, kennzeichen_typ="firmenweit")

    zeitpunkt = await ausloese_zeitpunkt(session, kennzeichen_typ="firmenweit")

    assert zeitpunkt == zeile.occurred_at


async def test_ausloese_zeitpunkt_nimmt_frueheste_zeile_ohne_zwischenzeitliches_aufheben(session):
    """Der harte Befund, der diesen Mechanismus nötig macht: ein zweiter,
    andersartiger automatischer Auslöser während bereits laufender Sperre darf die
    Frist NICHT zurücksetzen -- sonst würde jeder Folge-Trigger die 4h-Frist immer
    wieder neu starten lassen."""
    await setze_firmenweit(session, ausloeser="stufe2", begruendung="Erster Auslöser")
    erste_zeile = await _letzte_protokollzeile(session, kennzeichen_typ="firmenweit")
    await _backdatiere(session, erste_zeile, timedelta(hours=1))

    await setze_firmenweit(
        session, ausloeser="stop_orderbuch_abweichung", begruendung="Zweiter Auslöser"
    )

    zeitpunkt = await ausloese_zeitpunkt(session, kennzeichen_typ="firmenweit")

    assert zeitpunkt == erste_zeile.occurred_at


async def test_ausloese_zeitpunkt_nach_aufheben_und_neuem_setzen_ist_der_neue(session):
    await setze_strategie(session, "7", ausloeser="stufe1", begruendung="Erster Lauf")
    await hebe_strategie_auf(session, "7", entscheider="CRO", begruendung="Behoben")
    await setze_strategie(session, "7", ausloeser="stufe1", begruendung="Zweiter Lauf")
    neueste_zeile = await _letzte_protokollzeile(
        session, kennzeichen_typ="strategie", strategie_schluessel="7"
    )

    zeitpunkt = await ausloese_zeitpunkt(
        session, kennzeichen_typ="strategie", strategie_schluessel="7"
    )

    assert zeitpunkt == neueste_zeile.occurred_at


# --- Stufe 1 (24h, je Strategie) --------------------------------------------------


async def _strategie_mit_portfolio(session, client, *, strategie_schluessel: str) -> Strategy:
    portfolio_id = (await create_portfolio(client, currency="EUR", cash="10000"))["id"]
    strategy = Strategy(id=int(strategie_schluessel), gate_status="S3", portfolio_id=portfolio_id)
    session.add(strategy)
    await session.commit()
    return strategy


async def test_stufe1_eskaliert_bei_offener_position_nach_24h(
    session, session_factory, client, prices
):
    strategy = await _strategie_mit_portfolio(session, client, strategie_schluessel="101")
    btc = await asset_id(session, "BTC")
    session.add(Position(portfolio_id=strategy.portfolio_id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(50000)))
    await setze_strategie(session, "101", ausloeser="stufe1", begruendung="Test")
    zeile = await _letzte_protokollzeile(
        session, kennzeichen_typ="strategie", strategie_schluessel="101"
    )
    await _backdatiere(session, zeile, timedelta(hours=24, minutes=1))

    eskaliert = await pruefe_stufe1_fristen(session_factory)

    assert eskaliert == ["101"]
    zeilen = await _eskalationen(session)
    assert len(zeilen) == 1
    assert zeilen[0].kennzeichen_typ == "strategie"
    assert zeilen[0].strategie_schluessel == "101"
    assert zeilen[0].frist_typ == "stufe1_schliessung"
    assert zeilen[0].ausloese_zeitpunkt == zeile.occurred_at
    assert "offene Position" in zeilen[0].befund


async def test_stufe1_keine_eskalation_vor_ablauf_der_frist(
    session, session_factory, client, prices
):
    strategy = await _strategie_mit_portfolio(session, client, strategie_schluessel="102")
    btc = await asset_id(session, "BTC")
    session.add(Position(portfolio_id=strategy.portfolio_id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(50000)))
    await setze_strategie(session, "102", ausloeser="stufe1", begruendung="Test")
    zeile = await _letzte_protokollzeile(
        session, kennzeichen_typ="strategie", strategie_schluessel="102"
    )
    await _backdatiere(session, zeile, timedelta(hours=23))

    eskaliert = await pruefe_stufe1_fristen(session_factory)

    assert eskaliert == []
    assert await _eskalationen(session) == []


async def test_stufe1_keine_eskalation_wenn_position_bereits_geschlossen(
    session, session_factory, client, prices
):
    await _strategie_mit_portfolio(session, client, strategie_schluessel="103")
    await setze_strategie(session, "103", ausloeser="stufe1", begruendung="Test")
    zeile = await _letzte_protokollzeile(
        session, kennzeichen_typ="strategie", strategie_schluessel="103"
    )
    await _backdatiere(session, zeile, timedelta(hours=25))

    eskaliert = await pruefe_stufe1_fristen(session_factory)

    assert eskaliert == []
    assert await _eskalationen(session) == []


async def test_stufe1_eskaliert_fail_closed_ohne_verknuepftes_portfolio(session, session_factory):
    strategy = Strategy(id=104, gate_status="S3", portfolio_id=None)
    session.add(strategy)
    await setze_strategie(session, "104", ausloeser="stufe1", begruendung="Test")
    zeile = await _letzte_protokollzeile(
        session, kennzeichen_typ="strategie", strategie_schluessel="104"
    )
    await _backdatiere(session, zeile, timedelta(hours=25))

    eskaliert = await pruefe_stufe1_fristen(session_factory)

    assert eskaliert == ["104"]
    zeilen = await _eskalationen(session)
    assert "nicht entscheidbar" in zeilen[0].befund


async def test_stufe1_eskaliert_nur_einmal_je_auslosung(
    session, session_factory, client, prices
):
    strategy = await _strategie_mit_portfolio(session, client, strategie_schluessel="105")
    btc = await asset_id(session, "BTC")
    session.add(Position(portfolio_id=strategy.portfolio_id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(50000)))
    await setze_strategie(session, "105", ausloeser="stufe1", begruendung="Test")
    zeile = await _letzte_protokollzeile(
        session, kennzeichen_typ="strategie", strategie_schluessel="105"
    )
    await _backdatiere(session, zeile, timedelta(hours=25))

    erster_lauf = await pruefe_stufe1_fristen(session_factory)
    zweiter_lauf = await pruefe_stufe1_fristen(session_factory)

    assert erster_lauf == ["105"]
    assert zweiter_lauf == []
    assert len(await _eskalationen(session)) == 1


# --- Stufe 2 (4h, firmenweit) ------------------------------------------------------


async def _portfolio_mit_exposure(
    session, client, *, cash: str, btc_menge: str | None, name: str = "Main"
) -> int:
    portfolio_id = (await create_portfolio(client, name=name, currency="EUR", cash=cash))["id"]
    if btc_menge is not None:
        btc = await asset_id(session, "BTC")
        session.add(Position(portfolio_id=portfolio_id, asset_id=btc, quantity=D(btc_menge),
                             avg_cost=D(50000)))
        await session.commit()
    return portfolio_id


async def _setze_firmenweit_und_backdatiere(session, delta: timedelta, *, ausloeser="stufe2"):
    await setze_firmenweit(session, ausloeser=ausloeser, begruendung="Test")
    zeile = await _letzte_protokollzeile(session, kennzeichen_typ="firmenweit")
    await _backdatiere(session, zeile, delta)
    return zeile


def _eur_fx(sources) -> None:
    """EUR/USD(C) fest auf 1:1, damit die Testzahlen unten exakt aufgehen (gleiches
    Muster wie tests/test_risikoauszug.py)."""
    sources["yahoo"].add("EURUSD=X", "currency", exchange="CCY")
    sources["yahoo"].price("EURUSD=X", "1.0")


async def test_stufe2_eskaliert_bei_exposure_ueber_20_prozent_nach_4h(
    session, session_factory, client, market, prices
):
    _eur_fx(prices)
    # 0.1 BTC @ 50.000 = 5.000 EUR Exposure auf 10.000 EUR Cash -> NAV 15.000, 33,3%.
    await _portfolio_mit_exposure(session, client, cash="10000", btc_menge="0.1")
    zeile = await _setze_firmenweit_und_backdatiere(session, timedelta(hours=4, minutes=1))

    eskaliert = await pruefe_stufe2_fristen(session_factory, market)

    assert eskaliert is True
    zeilen = await _eskalationen(session)
    assert len(zeilen) == 1
    assert zeilen[0].kennzeichen_typ == "firmenweit"
    assert zeilen[0].strategie_schluessel is None
    assert zeilen[0].frist_typ == "stufe2_exposure_rueckfuehrung"
    assert zeilen[0].ausloese_zeitpunkt == zeile.occurred_at
    assert "33.33" in zeilen[0].befund
    assert "NAV=15000.00" in zeilen[0].befund


async def test_stufe2_keine_eskalation_unter_20_prozent(
    session, session_factory, client, market, prices
):
    _eur_fx(prices)
    # 0.02 BTC @ 50.000 = 1.000 EUR Exposure auf 10.000 EUR Cash -> NAV 11.000, 9,1%.
    await _portfolio_mit_exposure(session, client, cash="10000", btc_menge="0.02")
    await _setze_firmenweit_und_backdatiere(session, timedelta(hours=5))

    eskaliert = await pruefe_stufe2_fristen(session_factory, market)

    assert eskaliert is False
    assert await _eskalationen(session) == []


async def test_stufe2_keine_eskalation_vor_ablauf_der_frist(
    session, session_factory, client, market, prices
):
    _eur_fx(prices)
    await _portfolio_mit_exposure(session, client, cash="10000", btc_menge="0.1")
    await _setze_firmenweit_und_backdatiere(session, timedelta(hours=3))

    eskaliert = await pruefe_stufe2_fristen(session_factory, market)

    assert eskaliert is False
    assert await _eskalationen(session) == []


async def test_stufe2_fail_closed_wenn_kein_portfolio_vorhanden(
    session, session_factory, market
):
    await _setze_firmenweit_und_backdatiere(session, timedelta(hours=5))

    eskaliert = await pruefe_stufe2_fristen(session_factory, market)

    assert eskaliert is True
    zeilen = await _eskalationen(session)
    assert "nicht berechenbar" in zeilen[0].befund


async def test_stufe2_summiert_exposure_und_nav_ueber_alle_portfolios(
    session, session_factory, client, market, prices
):
    """Firmenweit heißt über alle Portfolios summiert, nicht nur das erste -- sonst
    würde eine zweite, selbst für sich betrachtet unauffällige Buchhaltungseinheit
    unsichtbar zur Gesamt-Exposure beitragen."""
    _eur_fx(prices)
    await _portfolio_mit_exposure(
        session, client, cash="10000", btc_menge=None, name="Portfolio A"
    )
    await _portfolio_mit_exposure(
        session, client, cash="10000", btc_menge="0.12", name="Portfolio B"
    )
    # NAV = 10.000 + (10.000 + 6.000) = 26.000; Exposure = 6.000 -> 23,08%.
    await _setze_firmenweit_und_backdatiere(session, timedelta(hours=5))

    eskaliert = await pruefe_stufe2_fristen(session_factory, market)

    assert eskaliert is True
    zeilen = await _eskalationen(session)
    assert "NAV=26000.00" in zeilen[0].befund
    assert "Brutto-Exposure=6000.00" in zeilen[0].befund


async def test_stufe2_eskaliert_nur_einmal_je_auslosung(
    session, session_factory, client, market, prices
):
    _eur_fx(prices)
    await _portfolio_mit_exposure(session, client, cash="10000", btc_menge="0.1")
    await _setze_firmenweit_und_backdatiere(session, timedelta(hours=5))

    erster_lauf = await pruefe_stufe2_fristen(session_factory, market)
    zweiter_lauf = await pruefe_stufe2_fristen(session_factory, market)

    assert erster_lauf is True
    assert zweiter_lauf is False
    assert len(await _eskalationen(session)) == 1
