"""Versand-Treiber Z7 "board_freigegeben" -> Z10 "versandt" (B-12, HED-84, Entwurf
Abschnitt 5). Prüfplan P-1.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.dbtypes import utcnow
from app.models import Portfolio, Strategy
from app.models.proposals import (
    STATE_BOARD_FREIGEGEBEN,
    STATE_STUFE1_LAEUFT,
    STATE_VERSANDT,
    STATE_WARTET_AUF_BOARD,
)
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.trading.proposals import Signal, create_proposal, transition
from app.trading.stufe2 import baue_versandengpass, pruefe_freigabe_nachweis
from app.trading.versand_treiber import _quote_waehrung, treibe_einmal
from app.trading.versandengpass import Versandengpass, VersandOrder
from tests.conftest import D, asset_id, create_portfolio


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


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
async def mit_schluessel(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)


async def _freigegebener_vorschlag(
    session, strategy, *, direction="long", mode="paper", **overrides
):
    felder = dict(
        signal_id="sig-1", strategy_id=strategy.id, instrument="BTC", direction=direction,
        size=D("0.01"), proposal_price=D(50000), stop_price=D(47500), proposal_kind="entry",
        signal_at=datetime.now(UTC),
    )
    felder.update(overrides)
    signal = Signal(**felder)
    proposal = await create_proposal(session, signal, mode=mode)
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice", board_action="approve"
    )
    proposal.approval_token = "token-1"
    proposal.expires_at = utcnow() + timedelta(hours=2)
    if "limit_price" not in overrides:
        proposal.limit_price = D(50000)
    await session.commit()
    return proposal


def test_quote_waehrung():
    assert _quote_waehrung("BTC/USDC") == "USDC"
    assert _quote_waehrung("BTC/USDT") == "USDT"
    assert _quote_waehrung("kaputt") is None


def _engpass_mit_echtem_freigabe_nachweis_und_bestandener_stufe2() -> Versandengpass:
    """Entkoppelt den Versand-Treiber-Test von der vollen B-2-Stufe-2-Prüfkette
    (Limits/Preisband/Kursfrische -- bereits Gegenstand von test_stufe2.py) und von
    ``MarketService``s Preis-Rekorder (löst sonst eine mit dieser minimalen
    Test-Fixture nicht erfüllbare Limit-Prüfung aus, siehe
    ``test_versandengpass_mit_echten_pruefern_und_gueltiger_freigabe_erreicht_stufe2``
    in test_stufe2.py, die genau dieselbe Fixture bewusst als "lehnt ehrlich ab"
    dokumentiert). Der Freigabe-Nachweis-Check (Z7->Z8, F-1 dieser Testdatei) bleibt
    echt -- das ist der Teil, den der Versand-Treiber tatsächlich auslöst."""

    async def freigabe_nachweis(session, order: VersandOrder) -> bool:
        return await pruefe_freigabe_nachweis(session, order)

    async def immer_bestanden(session, order: VersandOrder) -> bool:
        return True

    engpass = Versandengpass(
        freigabe_nachweis_pruefer=freigabe_nachweis, stufe2_pruefer=immer_bestanden
    )

    async def fake_sende(order):
        return "fake-gegenstellen-order-id"

    engpass._sende_an_gegenstelle = fake_sende
    return engpass


async def test_gueltiger_vorschlag_wird_versandt(
    mit_schluessel, session, session_factory, prices, strategy, btc
):
    proposal = await _freigegebener_vorschlag(session, strategy)
    engpass = _engpass_mit_echtem_freigabe_nachweis_und_bestandener_stufe2()

    versandt = await treibe_einmal(session_factory, engpass)

    assert versandt == 1
    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT
    assert proposal.gegenstellen_order_id == "fake-gegenstellen-order-id"
    assert proposal.sent_at is not None


async def test_short_wird_nicht_versandt(
    mit_schluessel, session, session_factory, market, prices, strategy, btc
):
    proposal = await _freigegebener_vorschlag(session, strategy, direction="short")
    engpass = baue_versandengpass(market)
    engpass._sende_an_gegenstelle = _nicht_aufrufen

    versandt = await treibe_einmal(session_factory, engpass)

    assert versandt == 0
    await session.refresh(proposal)
    assert proposal.state == STATE_BOARD_FREIGEGEBEN


async def test_ohne_grenzpreis_wird_nicht_versandt(
    mit_schluessel, session, session_factory, market, prices, strategy, btc
):
    proposal = await _freigegebener_vorschlag(session, strategy)
    proposal.limit_price = None
    await session.commit()
    engpass = baue_versandengpass(market)
    engpass._sende_an_gegenstelle = _nicht_aufrufen

    versandt = await treibe_einmal(session_factory, engpass)

    assert versandt == 0
    await session.refresh(proposal)
    assert proposal.state == STATE_BOARD_FREIGEGEBEN


async def test_live_modus_wird_nicht_versandt(
    mit_schluessel, session, session_factory, market, prices, strategy, btc
):
    proposal = await _freigegebener_vorschlag(session, strategy, mode="live")
    engpass = baue_versandengpass(market)
    engpass._sende_an_gegenstelle = _nicht_aufrufen

    versandt = await treibe_einmal(session_factory, engpass)

    assert versandt == 0
    await session.refresh(proposal)
    assert proposal.state == STATE_BOARD_FREIGEGEBEN


async def test_kein_boersensymbol_wird_nicht_versandt(
    mit_schluessel, session, session_factory, market, prices, strategy
):
    """Kein AssetSourceSymbol für 'okx' -> kein Versand, fail closed."""
    from app.models import Asset

    asset = Asset(
        symbol="GHOST", name="Ghost", asset_class="crypto", quote_currency="USD",
        quantity_step=D("0.00000001"), calendar="24/7",
    )
    session.add(asset)
    await session.flush()

    proposal = await _freigegebener_vorschlag(session, strategy, instrument="GHOST")
    engpass = baue_versandengpass(market)
    engpass._sende_an_gegenstelle = _nicht_aufrufen

    versandt = await treibe_einmal(session_factory, engpass)

    assert versandt == 0
    await session.refresh(proposal)
    assert proposal.state == STATE_BOARD_FREIGEGEBEN


async def _nicht_aufrufen(order):
    raise AssertionError(
        "_sende_an_gegenstelle darf für diesen Vorschlag nicht aufgerufen werden"
    )
