"""B-2 Stufe 1 -- Vorfilter vor Anzeige (Entwurf HED-36 Abschnitt 6.1, CRO-freigegeben).

Integrationstests gegen eine echte (SQLite-)Datenbank und gescriptete Marktdaten.
Siehe tests/test_risikolimits.py für die reinen Katalog-Tests.
"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.models import Portfolio, Position, Strategy
from app.models.proposals import STATE_STUFE1_ABGELEHNT
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.trading.proposals import Signal, create_proposal
from app.trading.stufe1 import (
    ProposalEingabe,
    _erhoehung_hat_passende_position,
    lauf_stufe1,
    sammle_kontext,
)
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


@pytest.mark.parametrize(
    "feld,wert,erwarteter_grund_teil",
    [
        ("direction", "sideways", "weder long noch short"),
        ("size", D(0), "Größe"),
        ("size", D(-1), "Größe"),
        ("proposal_price", D(0), "Vorschlagskurs"),
        ("proposal_price", D(-1), "Vorschlagskurs"),
        ("stop_price", D(0), "Stop-Kurs"),
        ("stop_price", D(-1), "Stop-Kurs"),
    ],
)
async def test_unplausibles_signal_wird_abgelehnt(
    session, market, prices, portfolio, strategy, btc, feld, wert, erwarteter_grund_teil
):
    """Schritt 1.1 (HED-65): Größe/Kurse <= 0 oder eine ungültige Richtung sind durch
    den frozen dataclass ProposalEingabe nicht abgefangen (Konstruktion gelingt
    trotzdem) -- die explizite Plausibilitätsprüfung muss das selbst ablehnen.
    Überschreibt das jeweilige Feld erst in der fertigen ``ProposalEingabe`` (nicht im
    ``Signal``), weil ``create_proposal`` für ``proposal_price`` bereits selbst durch
    null dividieren würde (``stop_distance_pct``) -- außerhalb des Umfangs hier."""
    signal = _signal(strategy.id, btc)
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = replace(_eingabe(signal, btc), **{feld: wert})
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "signal_unplausibel"
    assert erwarteter_grund_teil in result.rejection_reason_text
    assert result.limit_utilization is None  # vor Schritt 1.11 abgelehnt


async def test_kurs_nicht_lesbar_wird_in_stufe1_abgelehnt(
    session, market, sources, portfolio, strategy
):
    """Schritt 1.4 (HED-65): Kein Preis für das Vorschlagsinstrument gescriptet --
    keine Quelle in der Kette (okx/kraken/yahoo) kennt BTC/USDC -- Stufe 1 muss fail
    closed ablehnen, statt erst in sammle_kontext an einer anderen Stelle zu stolpern.
    Nutzt bewusst ``sources`` statt der ``prices``-Fixture (über die auch die
    ``btc``-Fixture läuft), sonst wäre für BTC/USDC längst ein Kurs gescriptet."""
    sources["okx"].add("BTC/USDC", currency="USDC")  # Instrument bekannt, aber kein Kurs
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
    assert result.rejection_reason_code == "kurs_nicht_lesbar"
    assert result.price_age_seconds is None
    assert result.limit_utilization is None  # vor Schritt 1.11 abgelehnt


async def test_kurs_lesbar_setzt_price_age_seconds(
    session, market, clock, prices, portfolio, strategy, btc
):
    """Positiver Gegenfall zu Schritt 1.4: ein lesbarer Kurs wird akzeptiert und das
    Kursalter eingefroren -- unabhängig davon, ob Stufe 1 später an einem anderen
    Limit scheitert."""
    prices["okx"].price("BTC/USDC", 50000, observed_at=clock.current)
    clock.advance(5)
    signal = _signal(strategy.id, btc)
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.price_age_seconds == 5


@pytest.mark.parametrize(
    "position_quantity,richtung,erwartet",
    [
        (None, "long", False),  # keine Position überhaupt
        (D(0), "long", False),  # Position glattgestellt (quantity 0 zählt nicht)
        (D("-0.01"), "long", False),  # bestehende Position in Gegenrichtung (Short)
        (D("0.01"), "short", False),  # Spiegelfall: Long-Position, Short-Erhöhung
        (D("0.01"), "long", True),  # passender Fall: gleiche Richtung
        (D("-0.01"), "short", True),  # passender Fall, Short-Seite
    ],
)
async def test_erhoehung_hat_passende_position(
    session, portfolio, btc, position_quantity, richtung, erwartet
):
    """Schritt 1.13 (HED-65), direkter Test der Hilfsfunktion statt über den vollen
    ``lauf_stufe1``-Pfad -- unabhängig vom Entwicklungsstand der übrigen Limits aus
    app.risikolimits (B-11/HED-83 hat ``mindest_barbestand`` inzwischen verdrahtet,
    andere Folgeissues wie Orderrate fehlen weiterhin). Siehe auch
    tests/test_risikolimits.py für dasselbe Vorgehen bei den reinen Katalogtests."""
    if position_quantity is not None:
        session.add(Position(portfolio_id=portfolio.id, asset_id=btc,
                             quantity=position_quantity, avg_cost=D(50000)))
        await session.commit()

    ergebnis = await _erhoehung_hat_passende_position(session, portfolio.id, btc, richtung)
    assert ergebnis is erwartet


async def test_lauf_stufe1_lehnt_erhoehung_ohne_passende_position_ab_sobald_limits_bestehen(
    session, market, prices, portfolio, strategy, btc, monkeypatch
):
    """Integrationstest für Schritt 1.13 über den vollen ``lauf_stufe1``-Pfad: die
    Katalogfunktion wird durch eine leere Liste ersetzt (Stand "alle Limits
    bestehen"), damit dieser Test unabhängig vom Entwicklungsstand der einzelnen
    Limits aus app.risikolimits bleibt und ausschließlich Schritt 1.13 selbst
    innerhalb von ``lauf_stufe1`` prüft."""
    import app.trading.stufe1 as stufe1_module

    monkeypatch.setattr(stufe1_module, "pruefe_alle_limits", lambda ctx: [])

    signal = _signal(strategy.id, btc, proposal_kind="increase")
    proposal = await create_proposal(session, signal, mode="paper")
    await session.commit()

    eingabe = _eingabe(signal, btc)
    result = await lauf_stufe1(
        session, market, strategy, proposal, eingabe, tranche="B1", tranche_eur=D(2000)
    )
    await session.commit()

    assert result.state == STATE_STUFE1_ABGELEHNT
    assert result.rejection_reason_code == "erhoehung_ohne_passende_position"


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


# --- Nachkaufen in den Verlust, HED-8 Tabelle 4.2 (HED-66) --------------------------------
# BTC/USDC steht in der ``prices``-Fixture fest auf 50.000 -- avg_cost weit darüber bzw.
# darunter erzeugt eindeutig Verlust bzw. Gewinn, unabhängig vom genauen EUR/USDC-Kurs.


async def test_sammle_kontext_erkennt_nachkauf_in_verlust_bei_erhoehung_mit_verlustposition(
    session, market, prices, portfolio, btc
):
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(90000)))  # weit über dem aktuellen Kurs -> Verlust
    await session.commit()

    signal = _signal(0, btc, proposal_kind="increase")  # strategy_id ungenutzt hier
    eingabe = _eingabe(signal, btc)
    ctx = await sammle_kontext(session, market, portfolio, eingabe, tranche="B1",
                               tranche_eur=D(2000))

    assert ctx.nachkauf_in_verlust_pruefbar is True
    assert ctx.position_unrealized_vor_trade_eur is not None
    assert ctx.position_unrealized_vor_trade_eur < 0


async def test_sammle_kontext_nachkauf_mit_gewinnposition_nicht_in_verlust(
    session, market, prices, portfolio, btc
):
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(1)))  # weit unter dem aktuellen Kurs -> Gewinn
    await session.commit()

    signal = _signal(0, btc, proposal_kind="increase")
    eingabe = _eingabe(signal, btc)
    ctx = await sammle_kontext(session, market, portfolio, eingabe, tranche="B1",
                               tranche_eur=D(2000))

    assert ctx.nachkauf_in_verlust_pruefbar is True
    assert ctx.position_unrealized_vor_trade_eur is not None
    assert ctx.position_unrealized_vor_trade_eur > 0


async def test_sammle_kontext_entry_ist_nicht_pruefbar_auch_mit_verlustposition(
    session, market, prices, portfolio, btc
):
    """Ein Entry (neues Instrument) ist per Definition kein Nachkauf -- selbst wenn
    (bei einem anderen Vorschlag) zufällig eine Verlustposition im selben Instrument
    bestünde, zählt das Limit nur bei ``proposal_kind == "increase"``."""
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(90000)))
    await session.commit()

    signal = _signal(0, btc, proposal_kind="entry")
    eingabe = _eingabe(signal, btc)
    ctx = await sammle_kontext(session, market, portfolio, eingabe, tranche="B1",
                               tranche_eur=D(2000))

    assert ctx.nachkauf_in_verlust_pruefbar is False
    assert ctx.position_unrealized_vor_trade_eur is None


async def test_sammle_kontext_erhoehung_in_gegenrichtung_ist_nicht_pruefbar(
    session, market, prices, portfolio, btc
):
    """Richtung passt nicht (Long-Position, Short-Vorschlag) -- Schritt 1.13 lehnt das
    gesondert ab, hier bleibt das Limit trivial unbroken statt eine falsche Position
    zu bewerten."""
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(90000)))
    await session.commit()

    signal = _signal(0, btc, proposal_kind="increase", direction="short",
                     stop_price=D(52500))
    eingabe = _eingabe(signal, btc)
    ctx = await sammle_kontext(session, market, portfolio, eingabe, tranche="B1",
                               tranche_eur=D(2000))

    assert ctx.nachkauf_in_verlust_pruefbar is False
    assert ctx.position_unrealized_vor_trade_eur is None
