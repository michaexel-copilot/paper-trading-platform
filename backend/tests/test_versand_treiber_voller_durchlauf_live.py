"""Vollständiger Durchlauf mit echtem Netzzugriff gegen OKX-Demo (B-12, HED-84,
Entwurf Abschnitt 13.2, **P-17 Abbruchkriterium**).

Anders als die Fake-/Mock-Tests in ``test_versand_treiber.py`` und
``test_stop_pflicht.py`` läuft hier die gesamte Kette Z7 "board_freigegeben" -> Z10
"versandt" (Versand-Treiber) -> Z11 "gefüllt" (Fill-Erkennung) -> Pflicht-Stop-
Platzierung -> täglicher Stop-Orderbuch-Abgleich mit echtem Netzzugriff auf
eea.okx.com. Nur ``_pruefe`` Schritt 7 (B-2-Stufe-2-Limitkette: Preisband, Kursfrische,
Risikolimits) ist entkoppelt -- dieselbe Begründung wie in
``test_versand_treiber.py::_engpass_mit_echtem_freigabe_nachweis_und_bestandener_stufe2``:
diese Limitkette ist bereits Gegenstand von ``test_stufe2.py``, P-17 prüft den
Versand-/Fill-/Stop-Pfad, nicht die Risikolimit-Inhalte.

Wie alle anderen Live-Tests: läuft nicht in der normalen Testrunde (``addopts = "-m
'not live' -q"``), nur explizit über ``pytest -m live``. Ohne OKX-Demo-Secret wird
übersprungen, nicht als Fehlschlag gewertet. Keine dieser Messungen ist eine
Performance- oder Renditemessung (Handbuch Abschnitt 7) -- es geht ausschließlich um
die technische Abnahme der vollen Kette.

**Abbruchkriterium P-17** (Entwurf 13.2): schlägt dieser Test fehl, ist die
Versand-/Fill-/Stop-Kette im freigegebenen Umfang nicht wie entworfen umsetzbar --
dann dem CRO einen geänderten Entwurf vorlegen, nicht improvisieren.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import ccxt.async_support as ccxt_async
import pytest
from sqlalchemy import select

from app.betriebsart import Betriebsart
from app.dbtypes import utcnow
from app.models import Portfolio, Strategy
from app.models.proposals import (
    STATE_BOARD_FREIGEGEBEN,
    STATE_GEFUELLT,
    STATE_STUFE1_LAEUFT,
    STATE_VERSANDT,
    STATE_WARTET_AUF_BOARD,
)
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.models.stop_order import StopOrderBuch
from app.schluessel import ExchangeCredential
from app.trading.papier import okx_demo
from app.trading.proposals import Signal, create_proposal, transition
from app.trading.stop_orderbuch_abgleich import pruefe_einmal
from app.trading.stop_pflicht import pruefe_fills_einmal
from app.trading.stufe2 import pruefe_freigabe_nachweis
from app.trading.versand_treiber import treibe_einmal
from app.trading.versandengpass import Versandengpass, VersandOrder
from tests.conftest import create_portfolio

SYMBOL = "BTC/USDC"
NICHT_AUSLOESENDER_STOP = Decimal("40000")
TOLERANZ = Decimal("0.001")
TOTMANNFRIST = timedelta(hours=26)
PFLICHT_STOP_NACHFRIST = timedelta(seconds=120)
MENGE = Decimal("0.001")


def _real_demo_env_vorhanden() -> bool:
    return bool(os.environ.get("OKX_DEMO_API_KEY") and os.environ.get("OKX_DEMO_API_SECRET"))


@pytest.fixture
def mit_echtem_demo_schluessel(betriebsart, monkeypatch):
    """Anders als ``mit_schluessel`` in ``test_versand_treiber.py``: setzt KEINE
    Fake-Zugangsdaten, sondern verlässt sich auf die bereits in der Umgebung
    vorhandenen echten OKX-Demo-Secrets (Board-bereitgestellt). Ohne sie: skip, kein
    Fehlschlag."""
    if not _real_demo_env_vorhanden():
        pytest.skip("Kein OKX-Demo-Secret in der Umgebung -- Test übersprungen.")
    betriebsart("papier")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)


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


def _demo_credential() -> ExchangeCredential:
    return ExchangeCredential(
        exchange="okx",
        api_key=os.environ["OKX_DEMO_API_KEY"],
        api_secret=os.environ["OKX_DEMO_API_SECRET"],
        passphrase=os.environ.get("OKX_DEMO_PASSPHRASE"),
        betriebsart=Betriebsart.PAPIER,
        simulated_trading_header=True,
    )


def _client_wie_okx_demo(credential: ExchangeCredential) -> ccxt_async.okx:
    """Duplikat wie in ``test_okx_demo_stop_live.py`` -- nur für die reine
    Marktpreis-Abfrage und die Aufräumaktion (Position glattstellen), kein
    Produktionspfad."""
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


def _engpass_echter_netzzugriff_entkoppelt_von_stufe2_limits() -> Versandengpass:
    """Freigabe-Nachweis (Z7->Z8) bleibt echt. Die Stufe-2-Limitkette
    (Preisband/Kursfrische/Risikolimits, Schritte 2.5-2.12 in
    ``app.trading.stufe2.pruefe_stufe2``) ist bereits Gegenstand eigener Tests
    (``test_stufe2.py``) und hier bewusst entkoppelt -- siehe Moduldokumentation oben.
    ``_sende_an_gegenstelle`` bleibt unverändert die echte Produktionsmethode."""

    async def freigabe_nachweis(session, order: VersandOrder) -> bool:
        return await pruefe_freigabe_nachweis(session, order)

    async def immer_bestanden(session, order: VersandOrder) -> bool:
        return True

    return Versandengpass(
        freigabe_nachweis_pruefer=freigabe_nachweis, stufe2_pruefer=immer_bestanden
    )


async def _freigegebener_einstieg(session, strategy, *, limit_price, stop_price, size):
    signal = Signal(
        signal_id="sig-p17", strategy_id=strategy.id, instrument="BTC", direction="long",
        size=size, proposal_price=limit_price, stop_price=stop_price, proposal_kind="entry",
        signal_at=datetime.now(UTC),
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice", board_action="approve"
    )
    proposal.approval_token = "token-p17"
    proposal.expires_at = utcnow() + timedelta(hours=2)
    proposal.limit_price = limit_price
    await session.commit()
    return proposal


@pytest.mark.live
async def test_p17_voller_durchlauf_einstieg_fill_stop_abgleich(
    mit_echtem_demo_schluessel, session, session_factory, strategy
):
    """P-17, Abbruchkriterium: Limit-Einstieg knapp über dem Markt platzieren (echte
    OKX-Demo-Order), auf Füllung warten, Fill-Erkennung (Z10->Z11), automatische
    Stop-Platzierung, Stop bei OKX wiederfinden, täglicher Abgleich bestanden,
    aufräumen (Stop stornieren, Position glattstellen)."""
    credential = _demo_credential()
    client_preis = _client_wie_okx_demo(credential)
    try:
        ticker = await client_preis.fetch_ticker(SYMBOL)
    finally:
        await client_preis.close()
    markt_ask = Decimal(str(ticker["ask"]))

    # Knapp über dem Ask -- die Order soll als Taker sofort gefüllt werden. OKX-Demo
    # lehnt Limit-Kaufaufträge weit über dem letzten Kurs mit Fehler 51137 ab
    # ("highest price limit for the buy leg") -- 0,2% über dem Ask liegt innerhalb
    # der beobachteten Bandbreite (empirisch: Band lag bei ~0,5% über dem letzten
    # Kurs, 14.10.2026).
    limit_preis = (markt_ask * Decimal("1.002")).quantize(Decimal("1"))

    proposal = await _freigegebener_einstieg(
        session, strategy, limit_price=limit_preis, stop_price=NICHT_AUSLOESENDER_STOP,
        size=MENGE,
    )
    engpass = _engpass_echter_netzzugriff_entkoppelt_von_stufe2_limits()

    stop_algo_id = None
    gefuellte_menge_final = Decimal(0)
    try:
        versandt = await treibe_einmal(session_factory, engpass)
        assert versandt == 1
        await session.refresh(proposal)
        assert proposal.state == STATE_VERSANDT
        assert proposal.gegenstellen_order_id is not None

        for _ in range(10):
            await pruefe_fills_einmal(
                session_factory, engpass, pflicht_stop_nachfrist=PFLICHT_STOP_NACHFRIST
            )
            await session.refresh(proposal)
            if proposal.state == STATE_GEFUELLT:
                break
            await asyncio.sleep(1)
        assert proposal.state == STATE_GEFUELLT, (
            "Einstieg nach 10 Versuchen (je 1s Abstand) nicht gefüllt -- P-17 nicht "
            "bestanden."
        )
        gefuellte_menge_final = proposal.gefuellte_menge
        assert gefuellte_menge_final == MENGE

        stop_zeile = await session.scalar(
            select(StopOrderBuch).where(StopOrderBuch.anlass_proposal_id == proposal.id)
        )
        assert stop_zeile is not None, "Pflicht-Stop wurde nicht angelegt"
        assert stop_zeile.zustand == "offen"
        assert stop_zeile.menge_soll == MENGE
        assert stop_zeile.side == "sell"
        stop_algo_id = stop_zeile.gegenstellen_algo_id
        assert stop_algo_id is not None

        gefunden = await okx_demo.lese_offene_algo_orders(credential, SYMBOL)
        treffer = next((o for o in gefunden if o.algo_id == stop_algo_id), None)
        assert treffer is not None, "Pflicht-Stop nicht bei OKX-Demo wiedergefunden"
        assert treffer.side == "sell"
        assert treffer.menge == MENGE

        bestanden = await pruefe_einmal(
            session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
        )
        assert bestanden is True, "Täglicher Stop-Orderbuch-Abgleich (P-17) nicht bestanden"
    finally:
        if stop_algo_id is not None:
            await okx_demo.storniere_algo_order(credential, stop_algo_id, SYMBOL)
        if gefuellte_menge_final > 0:
            # Position glattstellen -- Markt-Sell derselben Menge, direkter ccxt-
            # Aufruf wie in den anderen Live-Tests, keine Produktionsfunktion (reine
            # Aufräumaktion, kein Teil der B-12-Kette).
            klient = _client_wie_okx_demo(credential)
            try:
                await klient.create_order(
                    SYMBOL, "market", "sell", float(gefuellte_menge_final), None
                )
            finally:
                await klient.close()

    nach_storno = await okx_demo.lese_offene_algo_orders(credential, SYMBOL)
    assert all(o.algo_id != stop_algo_id for o in nach_storno)
