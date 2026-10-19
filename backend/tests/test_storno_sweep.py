"""Pfad 2 -- Storno-Sweep ruhender Einstiegsorders (Entwurf HED-36 Abschnitt 9.6,
CRO-Entscheidung HED-191 Abschnitt 1). Prüft ``sweep_ruhende_einstiegsorders`` selbst
sowie die beiden verdrahteten automatischen Auslöser.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

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
from app.trading.storno_sweep import sweep_ruhende_einstiegsorders
from app.trading.stufe2 import baue_versandengpass
from tests.conftest import create_portfolio


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


@pytest.fixture
async def mit_schluessel(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)


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
async def btc(session, prices):
    from tests.conftest import asset_id

    return await asset_id(session, "BTC")


async def _z7_vorschlag(session, strategy, *, signal_id="sig-z7", token="token-1"):
    signal = Signal(
        signal_id=signal_id, strategy_id=strategy.id, instrument="BTC", direction="long",
        size=Decimal("0.01"), proposal_price=Decimal(50000), stop_price=Decimal(47500),
        proposal_kind="entry", signal_at=datetime.now(UTC),
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice", board_action="approve"
    )
    proposal.approval_token = token
    proposal.expires_at = utcnow() + timedelta(hours=2)
    proposal.limit_price = Decimal(50000)
    await session.commit()
    return proposal


async def _z8_vorschlag(
    session, strategy, *, gegenstellen_order_id=None, signal_id="sig-z8", token="token-z8"
):
    proposal = await _z7_vorschlag(session, strategy, signal_id=signal_id, token=token)
    await transition(session, proposal, STATE_STUFE2_LAEUFT, actor="system")
    proposal.gegenstellen_order_id = gegenstellen_order_id
    await session.commit()
    return proposal


async def test_z7_proposal_wird_sofort_ueber_versand_treiber_storniert(
    mit_schluessel, session, session_factory, market, prices, strategy, btc
):
    proposal = await _z7_vorschlag(session, strategy)
    kennzeichen = await session.scalar(select(FirmenweitesSperrkennzeichen))
    kennzeichen.gesetzt = True
    await session.commit()

    engpass = baue_versandengpass(market)

    async def nicht_aufrufen(order):
        raise AssertionError("darf bei gesetzter Sperre nicht an die Gegenstelle gehen")

    engpass._sende_an_gegenstelle = nicht_aufrufen

    await sweep_ruhende_einstiegsorders(session_factory, engpass, market=market)

    await session.refresh(proposal)
    assert proposal.state == STATE_STUFE2_ABGELEHNT
    assert proposal.rejection_reason_code == "stufe2_abgelehnt_wegen_sperre"


async def test_z8_proposal_ohne_gegenstellen_id_wird_intern_storniert(
    mit_schluessel, session, session_factory, strategy, btc
):
    proposal = await _z8_vorschlag(session, strategy)
    kennzeichen = await session.scalar(select(FirmenweitesSperrkennzeichen))
    kennzeichen.gesetzt = True
    await session.commit()

    betroffen = await sweep_ruhende_einstiegsorders(session_factory, None)

    assert betroffen == 1
    await session.refresh(proposal)
    assert proposal.state == STATE_STUFE2_ABGELEHNT
    assert proposal.rejection_reason_code == "storniert_wegen_sperre"


async def test_z8_proposal_mit_gegenstellen_id_bleibt_unangetastet(
    mit_schluessel, session, session_factory, strategy, btc
):
    """1c: keine neue Gegenstellen-Fähigkeit -- ein Z8-Vorschlag MIT bereits gesetzter
    Gegenstellen-Id (sollte praktisch nicht vorkommen) ist kein intern abbrechbarer
    Fall und wird fail-closed übersprungen, nicht angefasst."""
    proposal = await _z8_vorschlag(session, strategy, gegenstellen_order_id="okx-already-sent")

    betroffen = await sweep_ruhende_einstiegsorders(session_factory, None)

    assert betroffen == 0
    await session.refresh(proposal)
    assert proposal.state == STATE_STUFE2_LAEUFT


async def test_ohne_sperre_bleibt_nichts_angefasst(
    mit_schluessel, session, session_factory, market, prices, strategy, btc
):
    """Der Sweep ist harmlos, wenn gar keine Sperre aktiv ist -- wichtig, weil er in
    den verdrahteten Aufrufern (schluesseltrennung_laufend, stop_orderbuch_abgleich)
    nur bei einem tatsächlichen Setzen ausgelöst wird, aber trotzdem fail-safe bleiben
    soll, falls ein künftiger Aufrufer ihn unconditional aufruft."""
    proposal = await _z7_vorschlag(session, strategy)
    engpass = baue_versandengpass(market)

    async def nicht_aufrufen(order):
        raise AssertionError("ohne Sperre sollte Stufe2 real geprüft werden, nicht gesendet")

    engpass._sende_an_gegenstelle = nicht_aufrufen

    await sweep_ruhende_einstiegsorders(session_factory, engpass, market=market)

    await session.refresh(proposal)
    # Ohne Sperre lehnt die echte Stufe-2-Prüfung (Limits/Preisband) ab oder sendet --
    # in jedem Fall NICHT über den Pfad-2/18-Sperren-Mechanismus.
    assert proposal.rejection_reason_code != "stufe2_abgelehnt_wegen_sperre"


async def test_engpass_none_ueberspringt_z7_aber_bereinigt_z8(
    mit_schluessel, session, session_factory, strategy, btc
):
    """Eine Installation ohne laufenden Matcher (F-3) hat keinen Engpass -- der Sweep
    bleibt dann strikt konservativ: kein Z7-Sweep (nichts wird dadurch versandt, was
    sonst nicht auch versandt worden wäre), aber der rein interne Z8-Teil läuft
    trotzdem."""
    z7 = await _z7_vorschlag(session, strategy)
    z8 = await _z8_vorschlag(session, strategy)
    kennzeichen = await session.scalar(select(FirmenweitesSperrkennzeichen))
    kennzeichen.gesetzt = True
    await session.commit()

    betroffen = await sweep_ruhende_einstiegsorders(session_factory, None)

    assert betroffen == 1
    await session.refresh(z7)
    await session.refresh(z8)
    assert z7.state == STATE_BOARD_FREIGEGEBEN  # unangetastet, kein Engpass verfügbar
    assert z8.state == STATE_STUFE2_ABGELEHNT
