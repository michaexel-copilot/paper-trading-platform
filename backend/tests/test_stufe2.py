"""B-2 Stufe 2 -- Nachprüfung unmittelbar vor dem Senden (Entwurf HED-36 Abschnitt 6.2,
CRO-freigegeben). Hängt an die zwei fail-closed Einhängepunkte des Versand-Engpasses
(HED-42) -- siehe tests/test_versandengpass.py für dessen eigene Tests.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.dbtypes import utcnow
from app.models import Portfolio, Strategy
from app.models.proposals import (
    STATE_BOARD_FREIGEGEBEN,
    STATE_STUFE1_LAEUFT,
    STATE_STUFE2_ABGELEHNT,
    STATE_STUFE2_LAEUFT,
    STATE_WARTET_AUF_BOARD,
)
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.trading.proposals import Signal, create_proposal, transition
from app.trading.stufe2 import baue_versandengpass, pruefe_freigabe_nachweis, pruefe_stufe2
from app.trading.versandengpass import VersandOrder
from tests.conftest import D, asset_id, create_portfolio


@pytest.fixture
async def portfolio(client, session) -> Portfolio:
    portfolio_id = (await create_portfolio(client, currency="EUR", cash="10000"))["id"]
    return await session.get(Portfolio, portfolio_id)


@pytest.fixture
async def strategy(session, portfolio) -> Strategy:
    s = Strategy(gate_status="S3", portfolio_id=portfolio.id)
    session.add(s)
    await session.flush()
    return s


@pytest.fixture
async def btc(session, prices) -> int:
    return await asset_id(session, "BTC")


@pytest.fixture
async def freigegebener_vorschlag(session, strategy, btc):
    """Ein Vorschlag, manuell bis Z7 'board_freigegeben' getrieben (wie
    tests/test_proposals.py) -- Stufe 1 selbst ist nicht Gegenstand dieser Datei,
    siehe tests/test_stufe1.py."""
    signal = Signal(
        signal_id="sig-1", strategy_id=strategy.id, instrument="BTC", direction="long",
        size=D("0.01"), proposal_price=D(50000), stop_price=D(47500), proposal_kind="entry",
        signal_at=datetime.now(UTC),
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice", board_action="approve"
    )
    proposal.approval_token = "token-1"
    proposal.expires_at = utcnow() + timedelta(hours=2)
    await session.commit()
    return proposal


def _order(proposal, **overrides) -> VersandOrder:
    felder = dict(
        gattung="einstieg", strategie_schluessel="s1", exchange="okx", symbol="BTC/USDT",
        side="buy", quantity=proposal.size, order_type="limit", proposal_id=proposal.id,
        limit_price=proposal.limit_price,
    )
    felder.update(overrides)
    return VersandOrder(**felder)


# --- pruefe_freigabe_nachweis (2.2 / 2.3) -------------------------------------------------


async def test_gueltiger_nachweis_startet_stufe2(session, freigegebener_vorschlag):
    ok = await pruefe_freigabe_nachweis(session, _order(freigegebener_vorschlag))
    await session.commit()
    assert ok is True
    await session.refresh(freigegebener_vorschlag)
    assert freigegebener_vorschlag.state == STATE_STUFE2_LAEUFT


async def test_ohne_approval_token_wird_abgelehnt(session, freigegebener_vorschlag):
    freigegebener_vorschlag.approval_token = None
    await session.commit()
    ok = await pruefe_freigabe_nachweis(session, _order(freigegebener_vorschlag))
    assert ok is False


async def test_verfallener_vorschlag_wird_abgelehnt(session, freigegebener_vorschlag):
    freigegebener_vorschlag.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    ok = await pruefe_freigabe_nachweis(session, _order(freigegebener_vorschlag))
    assert ok is False


async def test_bereits_verwendeter_nachweis_wird_abgelehnt(session, freigegebener_vorschlag):
    assert await pruefe_freigabe_nachweis(session, _order(freigegebener_vorschlag)) is True
    await session.commit()
    # Zweiter Versuch: Zustand ist nicht mehr board_freigegeben, sondern stufe2_laeuft.
    ok = await pruefe_freigabe_nachweis(session, _order(freigegebener_vorschlag))
    assert ok is False


async def test_grosse_passt_nicht_zum_nachweis(session, freigegebener_vorschlag):
    ok = await pruefe_freigabe_nachweis(
        session, _order(freigegebener_vorschlag, quantity=D("99"))
    )
    assert ok is False


# --- pruefe_stufe2 (2.5-2.12) --------------------------------------------------------------


async def _gestartet(session, proposal) -> None:
    assert await pruefe_freigabe_nachweis(session, _order(proposal)) is True
    await session.commit()


async def test_kurs_zu_alt_wird_abgelehnt(session, market, clock, prices, freigegebener_vorschlag):
    await _gestartet(session, freigegebener_vorschlag)
    # FakeSource liefert ohne festen observed_at sonst bei jedem Abruf einen "frischen"
    # Zeitstempel -- hier einmal fixieren, um Alter unabhängig vom Abrufzeitpunkt zu
    # simulieren (Schritt 2.6 misst gegen den Quell-Zeitstempel, nicht den Abruf).
    prices["okx"].price("BTC/USDT", 50000, observed_at=clock.current)
    clock.advance(31)  # über die 30s-Kursfrische-Grenze für Krypto
    ok = await pruefe_stufe2(session, market, _order(freigegebener_vorschlag))
    await session.commit()
    assert ok is False
    await session.refresh(freigegebener_vorschlag)
    assert freigegebener_vorschlag.state == STATE_STUFE2_ABGELEHNT
    assert freigegebener_vorschlag.rejection_reason_code == "kursfrische"


async def test_kurs_ausserhalb_band_wird_abgelehnt(
    session, market, prices, freigegebener_vorschlag
):
    await _gestartet(session, freigegebener_vorschlag)
    prices["okx"].price("BTC/USDT", 55000)  # weit außerhalb des Bands um 50.000
    ok = await pruefe_stufe2(session, market, _order(freigegebener_vorschlag))
    await session.commit()
    assert ok is False
    await session.refresh(freigegebener_vorschlag)
    assert freigegebener_vorschlag.rejection_reason_code == "preisband"


async def test_kurs_unter_dem_stop_wird_abgelehnt(
    session, market, prices, freigegebener_vorschlag
):
    """Kurs noch im (sehr weiten) Band, aber bereits unter dem Stop -- Bedingung (ii)
    greift unabhängig von Bedingung (i), Abschnitt 6.3."""
    await _gestartet(session, freigegebener_vorschlag)
    prices["okx"].price("BTC/USDT", 47000)  # unter Stop 47.500
    ok = await pruefe_stufe2(session, market, _order(freigegebener_vorschlag))
    await session.commit()
    assert ok is False
    await session.refresh(freigegebener_vorschlag)
    assert freigegebener_vorschlag.rejection_reason_code in ("preisband", "jenseits_stop")


async def test_veraenderte_groesse_wird_abgelehnt(
    session, market, prices, freigegebener_vorschlag
):
    await _gestartet(session, freigegebener_vorschlag)
    ok = await pruefe_stufe2(
        session, market, _order(freigegebener_vorschlag, quantity=D("0.5"))
    )
    await session.commit()
    assert ok is False
    await session.refresh(freigegebener_vorschlag)
    assert freigegebener_vorschlag.rejection_reason_code == "groesse_veraendert"


async def test_alle_stufe2_eigenen_pruefungen_bestehen_aber_katalog_lehnt_ehrlich_ab(
    session, market, prices, freigegebener_vorschlag
):
    """Preis frisch, im Band, jenseits Stop, Größe passt -- genau wie Stufe 1
    (tests/test_stufe1.py) bricht die gemeinsame Katalogprüfung trotzdem ehrlich an
    einer heute nicht verdrahteten/nicht historisierten Kennzahl (Festlegung 6.1.1:
    dieselbe Quelle wie Stufe 1, kein eigenes, laxeres Stufe-2-Limitset)."""
    await _gestartet(session, freigegebener_vorschlag)
    ok = await pruefe_stufe2(session, market, _order(freigegebener_vorschlag))
    await session.commit()
    assert ok is False
    await session.refresh(freigegebener_vorschlag)
    assert freigegebener_vorschlag.state == STATE_STUFE2_ABGELEHNT
    assert freigegebener_vorschlag.stage2_limit_utilization is not None


async def test_nicht_proposal_gebundene_order_prueft_nur_kurs(session, market, prices):
    """Stornierung/Stop/Risikoabbau haben kein Proposal -- Mindestanforderung bleibt
    ein lesbarer, frischer Kurs (Entwurf 6.2 gilt wörtlich für board-freigegebene
    Einstiege/Erhöhungen)."""
    order = VersandOrder(
        gattung="stornierung", strategie_schluessel="s1", exchange="okx", symbol="BTC/USDT",
        side="sell", quantity=D("0.01"), order_type="market",
    )
    ok = await pruefe_stufe2(session, market, order)
    assert ok is True


# --- Ende-zu-Ende über den echten Versand-Engpass -----------------------------------------


@pytest.fixture
def mit_schluessel(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)


async def test_versandengpass_mit_echten_pruefern_lehnt_ohne_freigabe_ab(
    mit_schluessel, session, market, prices
):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()
    engpass = baue_versandengpass(market)
    order = VersandOrder(
        gattung="einstieg", strategie_schluessel="s1", exchange="okx", symbol="BTC/USDT",
        side="buy", quantity=D("0.01"), order_type="limit", proposal_id=None,
    )
    ergebnis = await engpass.send(session, order)
    assert ergebnis.angenommen is False
    assert ergebnis.ablehnungsgrund == "kein_gueltiger_freigabe_nachweis"


async def test_versandengpass_mit_echten_pruefern_und_gueltiger_freigabe_erreicht_stufe2(
    mit_schluessel, session, market, prices, freigegebener_vorschlag
):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()
    engpass = baue_versandengpass(market)
    ergebnis = await engpass.send(session, _order(freigegebener_vorschlag))
    assert ergebnis.angenommen is False
    # Der Freigabe-Nachweis besteht, Stufe 2 selbst lehnt ehrlich am Katalog ab --
    # nicht mehr am generischen fail-closed Standard aus HED-42.
    assert ergebnis.ablehnungsgrund == "stufe2_nicht_bestanden"
