"""B-2 Stufe 1 -- Vorfilter vor Anzeige (Entwurf HED-36 Abschnitt 6.1, CRO-freigegeben).

Integrationstests gegen eine echte (SQLite-)Datenbank und gescriptete Marktdaten.
Siehe tests/test_risikolimits.py für die reinen Katalog-Tests.
"""

from datetime import UTC, datetime

import pytest

from app.models import Portfolio, Strategy
from app.models.proposals import STATE_STUFE1_ABGELEHNT
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.trading.proposals import Signal, create_proposal
from app.trading.stufe1 import ProposalEingabe, lauf_stufe1
from tests.conftest import D, asset_id, create_portfolio


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    """Normalzustand einer laufenden Installation (Konvention aus test_proposals.py,
    HED-47): das firmenweite Kennzeichen existiert und ist offen, sonst würde
    ``create_proposal`` seit HED-47 jedes Signal hier fail-closed unterdrücken, bevor
    Stufe 1 (Gegenstand dieser Datei) überhaupt läuft. Commit statt Flush, damit die
    Schreibtransaktion nicht offen bleibt, während ``client`` (eigene Verbindung zum
    selben SQLite-File) in einer anderen Fixture schreibt -- sonst 'database is
    locked'."""
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


def _signal(strategy_id: int, btc_id: int, **overrides) -> Signal:
    felder = dict(
        signal_id="sig-1",
        strategy_id=strategy_id,
        instrument="BTC",
        direction="long",
        size=D("0.01"),
        proposal_price=D(50000),
        stop_price=D(47500),  # 5% Stop-Abstand
        proposal_kind="entry",
        signal_at=datetime.now(UTC),
    )
    felder.update(overrides)
    return Signal(**felder)


def _eingabe(signal: Signal, asset_id_: int) -> ProposalEingabe:
    stop_distance = abs(signal.proposal_price - signal.stop_price)
    return ProposalEingabe(
        instrument_asset_id=asset_id_,
        direction=signal.direction,
        size=signal.size,
        proposal_price=signal.proposal_price,
        stop_price=signal.stop_price,
        stop_distance=stop_distance,
        stop_distance_pct=stop_distance / signal.proposal_price,
        proposal_kind=signal.proposal_kind,
    )


async def test_frisches_portfolio_lehnt_ehrlich_an_fehlender_snapshot_historie_ab(
    session, market, prices, portfolio, strategy, btc
):
    """Dokumentiert den tatsächlichen Stand: NAV/Risiko-je-Trade etc. werden korrekt
    berechnet und eingefroren, aber ein frisch angelegtes Portfolio hat noch keinen
    Wert-Snapshot von vor heute -- Tagesverlust ist dann nicht berechenbar und damit
    fail closed gebrochen (Festlegung 6.1.3), vor jedem noch nicht verdrahteten Limit
    aus app.risikolimits. Kein Fehler dieser Prüfung, sondern ihr Zweck."""
    signal = _signal(strategy.id, btc)
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "tagesverlust"
    assert result.nav == D(10000)
    assert result.risk_eur is not None
    assert result.limit_utilization is not None
    namen = {zeile["limit"] for zeile in result.limit_utilization}
    assert "risiko_je_trade" in namen
    assert "mindest_barbestand" in namen


async def test_echter_limitbruch_geht_den_nicht_verdrahteten_limits_vor(
    session, market, prices, portfolio, strategy, btc
):
    """Risiko je Trade bricht hier zuerst (Katalogreihenfolge), nicht erst am
    unverdrahteten Mindest-Barbestand -- zeigt, dass ein echter Bruch Vorrang hat."""
    signal = _signal(
        strategy.id, btc,
        size=D("1"),  # weit über jedes Risikobudget: 1 BTC x 2.500 Stop-Abstand
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "risiko_je_trade"


async def test_limit_price_wird_bei_ablehnung_nicht_gesetzt(
    session, market, prices, portfolio, strategy, btc
):
    """Der Grenzpreis (Abschnitt 6.3) wird nur eingefroren, wenn Stufe 1 bestanden
    ist -- er gehört zur Freigabe-Anzeige, nicht zu einer Ablehnung."""
    signal = _signal(strategy.id, btc)
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.limit_price is None


async def test_long_mit_stop_ueber_vorschlagskurs_wird_abgelehnt(
    session, market, prices, portfolio, strategy, btc
):
    """Schritt 1.2 (HED-64): ein Long mit Stop ÜBER dem Vorschlagskurs ist verkehrt
    liegend, auch wenn der (vorzeichenlose) Stop-Abstand positiv und scheinbar
    plausibel ist -- muss vor jeder Limitberechnung abgelehnt werden, nicht erst an
    einem der nachgelagerten Limits (z. B. risiko_je_trade) hängen bleiben."""
    signal = _signal(
        strategy.id, btc,
        direction="long",
        proposal_price=D(50000),
        stop_price=D(52500),  # über dem Vorschlagskurs -- falsche Seite für Long
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "stop_falsche_seite"
    assert result.limit_utilization is None  # vor Schritt 1.11 abgelehnt


async def test_short_mit_stop_unter_vorschlagskurs_wird_abgelehnt(
    session, market, prices, portfolio, strategy, btc
):
    """Schritt 1.2 (HED-64), Spiegelfall: Short mit Stop UNTER dem Vorschlagskurs."""
    signal = _signal(
        strategy.id, btc,
        direction="short",
        proposal_price=D(50000),
        stop_price=D(47500),  # unter dem Vorschlagskurs -- falsche Seite für Short
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "stop_falsche_seite"


async def test_stop_exakt_auf_vorschlagskurs_wird_abgelehnt(
    session, market, prices, portfolio, strategy, btc
):
    """Grenzfall ohne Kulanz (dieselbe Formel wie Schritt 2.11, ``jenseits_des_stops``):
    Stop == Vorschlagskurs ist auf keiner Seite, also auch nicht auf der richtigen."""
    signal = _signal(
        strategy.id, btc,
        direction="long",
        proposal_price=D(50000),
        stop_price=D(50000),
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "stop_falsche_seite"


async def test_strategie_ohne_portfolio_wird_fail_closed_abgelehnt(session, market, prices):
    """Eine Strategie ohne Portfolio-Verknüpfung (HED-44-Erweiterung von HED-41) kann
    keinen Vorschlag durch Stufe 1 bringen -- siehe app.models.strategies.Strategy."""
    strategy = Strategy(gate_status="S3", portfolio_id=None)
    session.add(strategy)
    await session.flush()
    btc_id = await asset_id(session, "BTC")
    signal = _signal(strategy.id, btc_id)
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc_id)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "kein_verknuepftes_portfolio"
