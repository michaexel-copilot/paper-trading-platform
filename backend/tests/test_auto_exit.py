"""Automatischer strategiegetriebener Ausstieg (Entwurf HED-36 Abschnitt 10,
CRO-freigegeben). Testpfade 22 (Reversal-Aufspaltung) und 23 (eskaliert_kursfrische)
aus Abschnitt 9.6, plus A1-A6/B1-B3 der Prüfkette aus Abschnitt 10.6.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Portfolio, Position, Strategy
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen
from app.trading import auto_exit as auto_exit_module
from app.trading.auto_exit import AusstiegsSignal, AutoExitFehler, fuehre_automatischen_ausstieg_aus
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


async def _setze_position(session, portfolio_id: int, asset_id_: int, quantity) -> None:
    session.add(
        Position(
            portfolio_id=portfolio_id,
            asset_id=asset_id_,
            quantity=D(quantity),
            avg_cost=D("50000"),
        )
    )
    await session.flush()


def _signal(strategy_id: int, **overrides) -> AusstiegsSignal:
    felder = dict(
        signal_id="exit-1",
        strategy_id=strategy_id,
        instrument="BTC",
        exchange="okx",
        symbol="BTC/USDT",
        side="sell",
        size=D("0.01"),
        signal_at=datetime.now(UTC),
    )
    felder.update(overrides)
    return AusstiegsSignal(**felder)


class _StufePruefer:
    """Injizierbarer stufe2_pruefer für den generischen Versandengpass (HED-42) --
    entkoppelt diese Tests von der echten Kursfrische-/Marktdaten-Infrastruktur
    (HED-44), um ausschließlich die Prüfkette aus Entwurf 10.6 (A1-A6) zu prüfen."""

    def __init__(self, ergebnis: bool = True):
        self.ergebnis = ergebnis
        self.aufgerufen_mit: list[VersandOrder] = []

    async def __call__(self, session, order: VersandOrder) -> bool:
        self.aufgerufen_mit.append(order)
        return self.ergebnis


class _FakeEngpass(Versandengpass):
    """Wie Versandengpass, aber mit einem echten Sendeerfolg statt NotImplementedError
    -- B-6 (OKX-Orderpfad) existiert noch nicht, siehe app.trading.versandengpass."""

    async def _sende_an_gegenstelle(self, order: VersandOrder) -> str:
        return "fake-gegenstellen-order-id"


@pytest.fixture
def mit_schluessel(betriebsart, monkeypatch):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)


# --- A1/A2: keine offene Position --------------------------------------------------


async def test_ohne_offene_position_eskaliert_als_einzelfreigabe_fall(
    mit_schluessel, session, market, prices, strategy
):
    """A2 ohne erklärte Gegenposition: kein automatischer Weg, einen Vorschlag zu
    bilden (kein Preis/Stop geliefert) -- Entwurf 10.2 "Als Einzelfreigabe-Fall
    behandeln" wird über Eskalation an CRO+Board erreicht, nicht über einen stillen
    Log-Eintrag (CRO-Nachforderung HED-67, Punkt 3)."""
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id)
    )
    assert ergebnis.eintrag.result == "eskaliert_keine_position"
    assert ergebnis.eintrag.escalation_result == "keine_offene_position"
    assert ergebnis.eintrag.escalation_recipients == ["cro", "board"]
    assert ergebnis.eintrag.escalation_at is not None
    assert ergebnis.eintrag.is_reversal is False
    assert ergebnis.gegenposition is None


async def test_ohne_offene_position_mit_erklaerter_gegenposition_deckt_teil2_die_einzelfreigabe(
    mit_schluessel, session, market, prices, strategy
):
    """A2 mit erklärter Gegenposition (Reversal-Signal, aber Teil 1 hat nichts zu
    schließen): Teil 2 entsteht trotzdem und deckt die Einzelfreigabe bereits ab --
    keine zusätzliche Eskalation nötig (CRO-Nachforderung HED-67, Punkte 2+3)."""
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    signal = _signal(
        strategy.id,
        side="sell",
        size=D("0.1"),
        gegenposition_size=D("0.07"),
        gegenposition_price=D(49000),
        gegenposition_stop_price=D(50500),
    )
    ergebnis = await fuehre_automatischen_ausstieg_aus(session, market, engpass, signal)
    assert ergebnis.eintrag.result == "abgelehnt_groesse"
    assert ergebnis.eintrag.escalation_at is None
    assert ergebnis.gegenposition is not None
    assert ergebnis.gegenposition.direction == "short"
    assert ergebnis.gegenposition.size == D("0.07")
    assert ergebnis.eintrag.reversal_counter_proposal_id == ergebnis.gegenposition.id


# --- A3/A6: Richtung ist nicht schließend ------------------------------------------


async def test_order_in_erhoehender_richtung_wird_abgelehnt(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "0.05")  # long
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    # side="buy" auf einer Long-Position ist eine Erhöhung, keine Schließung.
    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="buy")
    )
    assert ergebnis.eintrag.result == "abgelehnt_richtung"
    assert ergebnis.eintrag.position_before == D("0.05")


# --- A4: Größe wird auf die offene Position begrenzt, kein Reversal ---------------


async def test_groesse_wird_auf_offene_position_begrenzt(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "0.05")  # long
    pruefer = _StufePruefer(True)
    engpass = _FakeEngpass(stufe2_pruefer=pruefer)

    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="sell", size=D("0.2"))
    )

    assert ergebnis.eintrag.result == "versandt"
    assert ergebnis.eintrag.size_was_capped is True
    assert ergebnis.eintrag.size_capped_to == D("0.05")
    assert ergebnis.eintrag.position_before == D("0.05")
    assert ergebnis.eintrag.position_after == D("0")
    assert ergebnis.eintrag.exit_size == D("0.2")  # ursprünglich angeforderte Größe
    assert ergebnis.eintrag.is_reversal is False
    assert ergebnis.gegenposition is None
    # Die tatsächlich an den Versand-Engpass gereichte Order ist die begrenzte Größe.
    assert pruefer.aufgerufen_mit[0].quantity == D("0.05")


async def test_volle_schliessung_ohne_begrenzung(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "-0.02")  # short
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))

    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="buy", size=D("0.02"))
    )

    assert ergebnis.eintrag.result == "versandt"
    assert ergebnis.eintrag.size_was_capped is False
    assert ergebnis.eintrag.size_capped_to is None
    assert ergebnis.eintrag.position_before == D("-0.02")
    assert ergebnis.eintrag.position_after == D("0")
    assert ergebnis.eintrag.order_direction == "kauf"
    assert ergebnis.eintrag.position_direction_closed == "short"


# --- Reversal (Entwurf 10.4, Testpfad 22) ------------------------------------------


async def test_reversal_spaltet_in_schliessung_und_gegenposition(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "0.03")  # long
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))

    signal = _signal(
        strategy.id,
        side="sell",
        size=D("0.1"),
        gegenposition_size=D("0.07"),
        gegenposition_price=D(49000),
        gegenposition_stop_price=D(50500),
    )
    ergebnis = await fuehre_automatischen_ausstieg_aus(session, market, engpass, signal)

    # Teil 1: Schließung, nie mehr als die offene Position.
    assert ergebnis.eintrag.result == "versandt"
    assert ergebnis.eintrag.is_reversal is True
    assert ergebnis.eintrag.position_before == D("0.03")
    assert ergebnis.eintrag.position_after == D("0")

    # Teil 2: eigener, unabhängiger Vorschlag mit normaler Einzelfreigabe (Z0..),
    # nicht automatisch versandt.
    assert ergebnis.gegenposition is not None
    assert ergebnis.gegenposition.direction == "short"
    assert ergebnis.gegenposition.size == D("0.07")
    assert ergebnis.gegenposition.state == "stufe1_laeuft" or ergebnis.gegenposition.state in (
        "entstanden",
        "stufe1_laeuft",
    )
    assert ergebnis.eintrag.reversal_counter_proposal_id == ergebnis.gegenposition.id


async def test_reversal_teil2_entsteht_auch_wenn_teil1_an_a1_scheitert(
    mit_schluessel, session, market, prices, strategy, portfolio, btc, monkeypatch
):
    """Teil 1 und Teil 2 sind nicht verkettet (Entwurf 10.4 Festlegung 1) -- auch
    wenn Teil 1 an A1 (Position nicht lesbar) scheitert, muss das von der Strategie
    erklärte Gegenposition-Vorhaben (Teil 2) entstehen (CRO-Nachforderung HED-67,
    Punkt 2)."""

    async def kaputt(session_, portfolio_id, asset_id_):
        raise RuntimeError("Positionslese fehlgeschlagen (Testsimulation)")

    monkeypatch.setattr("app.trading.auto_exit._offene_position", kaputt)
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    signal = _signal(
        strategy.id,
        side="sell",
        size=D("0.1"),
        gegenposition_size=D("0.07"),
        gegenposition_price=D(49000),
        gegenposition_stop_price=D(50500),
    )
    ergebnis = await fuehre_automatischen_ausstieg_aus(session, market, engpass, signal)

    assert ergebnis.eintrag.result == "eskaliert_kursfrische"
    assert ergebnis.eintrag.escalation_result == "position_nicht_lesbar"
    assert ergebnis.gegenposition is not None
    assert ergebnis.gegenposition.direction == "short"
    assert ergebnis.gegenposition.size == D("0.07")
    assert ergebnis.eintrag.reversal_counter_proposal_id == ergebnis.gegenposition.id


async def test_reversal_teil2_entsteht_auch_wenn_teil1_an_a3_scheitert(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    """Wie oben, aber Teil 1 scheitert an A3 (falsche Richtung -- Order würde die
    bestehende Position erhöhen statt schließen)."""
    await _setze_position(session, portfolio.id, btc, "0.03")  # long
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    signal = _signal(
        strategy.id,
        side="buy",  # auf Long eine Erhöhung, keine Schließung -> A3 schlägt fehl
        size=D("0.1"),
        gegenposition_size=D("0.07"),
        gegenposition_price=D(49000),
        gegenposition_stop_price=D(50500),
    )
    ergebnis = await fuehre_automatischen_ausstieg_aus(session, market, engpass, signal)

    assert ergebnis.eintrag.result == "abgelehnt_richtung"
    assert ergebnis.gegenposition is not None
    assert ergebnis.eintrag.reversal_counter_proposal_id == ergebnis.gegenposition.id


async def test_reversal_fehlt_preis_eskaliert_nicht_sondern_wirft(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "0.03")
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    signal = _signal(strategy.id, side="sell", gegenposition_size=D("0.05"))
    with pytest.raises(AutoExitFehler):
        await fuehre_automatischen_ausstieg_aus(session, market, engpass, signal)


# --- Zweite Positionslese zum Versandzeitpunkt (Entwurf 10.2 Festlegung A4 letzter
# Satz / 10.6, CRO-Nachforderung HED-67 Punkt 1) --------------------------------------


async def test_position_schrumpft_zwischen_a1_und_versand_wird_am_versand_erneut_begrenzt(
    mit_schluessel, session, market, prices, strategy, portfolio, btc, monkeypatch
):
    """Die Position hat sich zwischen der A1-Lesung und dem tatsächlichen Versand
    weiter verkleinert (z. B. ein Teil-Stop hat in der Zwischenzeit gefüllt) -- die
    harte Obergrenze wird ein zweites Mal geprüft, die tatsächlich gesendete Order
    ist die neu begrenzte, kleinere Größe."""
    await _setze_position(session, portfolio.id, btc, "0.05")  # long, A1 liest 0.05
    aufrufe = {"n": 0}
    orig = auto_exit_module._offene_position

    async def schrumpfend(session_, portfolio_id, asset_id_):
        aufrufe["n"] += 1
        if aufrufe["n"] == 1:
            return await orig(session_, portfolio_id, asset_id_)
        return D("0.02")  # zum Versandzeitpunkt nur noch 0.02 offen

    monkeypatch.setattr(auto_exit_module, "_offene_position", schrumpfend)
    pruefer = _StufePruefer(True)
    engpass = _FakeEngpass(stufe2_pruefer=pruefer)

    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="sell", size=D("0.05"))
    )

    assert ergebnis.eintrag.result == "versandt"
    assert ergebnis.eintrag.size_was_capped is True
    assert ergebnis.eintrag.size_capped_to == D("0.02")
    assert ergebnis.eintrag.position_after == D("0")
    assert pruefer.aufgerufen_mit[0].quantity == D("0.02")


async def test_position_schliesst_vollstaendig_zwischen_a1_und_versand_wird_abgelehnt(
    mit_schluessel, session, market, prices, strategy, portfolio, btc, monkeypatch
):
    """Die Position ist zwischen A1 und Versand bereits vollständig geschlossen --
    keine automatische Order wird gesendet, Ergebnis bleibt fail closed statt eine
    Order mit Größe null zu versuchen."""
    await _setze_position(session, portfolio.id, btc, "0.05")  # long
    aufrufe = {"n": 0}
    orig = auto_exit_module._offene_position

    async def geschlossen(session_, portfolio_id, asset_id_):
        aufrufe["n"] += 1
        if aufrufe["n"] == 1:
            return await orig(session_, portfolio_id, asset_id_)
        return D("0")

    monkeypatch.setattr(auto_exit_module, "_offene_position", geschlossen)
    pruefer = _StufePruefer(True)
    engpass = _FakeEngpass(stufe2_pruefer=pruefer)

    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="sell", size=D("0.05"))
    )

    assert ergebnis.eintrag.result == "abgelehnt_groesse"
    assert pruefer.aufgerufen_mit == []


async def test_position_dreht_zwischen_a1_und_versand_wird_nicht_gesendet(
    mit_schluessel, session, market, prices, strategy, portfolio, btc, monkeypatch
):
    """Die Position hat zwischen A1 und Versand die Seite gewechselt (z. B. durch
    einen zwischenzeitlichen Fill in Gegenrichtung) -- eine Order in der
    ursprünglich schließenden Richtung würde jetzt Risiko erhöhen statt senken und
    darf nicht gesendet werden."""
    await _setze_position(session, portfolio.id, btc, "0.05")  # long, A1 liest long
    aufrufe = {"n": 0}
    orig = auto_exit_module._offene_position

    async def gedreht(session_, portfolio_id, asset_id_):
        aufrufe["n"] += 1
        if aufrufe["n"] == 1:
            return await orig(session_, portfolio_id, asset_id_)
        return D("-0.03")  # zum Versandzeitpunkt bereits short

    monkeypatch.setattr(auto_exit_module, "_offene_position", gedreht)
    pruefer = _StufePruefer(True)
    engpass = _FakeEngpass(stufe2_pruefer=pruefer)

    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="sell", size=D("0.05"))
    )

    assert ergebnis.eintrag.result == "abgelehnt_richtung"
    assert pruefer.aufgerufen_mit == []


# --- B1-B3 Kursfrische -> Eskalation (Entwurf 10.3, Testpfad 23) -------------------


async def test_veralteter_kurs_eskaliert_statt_auszufuehren(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "0.01")
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(False))  # simuliert Kursfrische-Fehlschlag

    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="sell", size=D("0.01"))
    )

    assert ergebnis.eintrag.result == "eskaliert_kursfrische"
    assert ergebnis.eintrag.escalation_result == "kursfrische"
    assert ergebnis.eintrag.escalation_at is not None
    assert ergebnis.eintrag.escalation_deadline == ergebnis.eintrag.escalation_at + timedelta(
        minutes=15
    )
    assert ergebnis.eintrag.escalation_recipients == ["cro", "board"]
    # Die Position bleibt unverändert -- der börsenseitige Stop (B-12) wirkt weiter.
    assert ergebnis.eintrag.position_after == ergebnis.eintrag.position_before


# --- Sperrkennzeichen: gelesen und protokolliert, aber nicht gatend (Tabelle 9.3) -


async def test_gesetztes_sperrkennzeichen_blockiert_ausstieg_nicht_wird_aber_protokolliert(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "0.01")
    zeile = await session.scalar(select(FirmenweitesSperrkennzeichen))
    zeile.gesetzt = True
    session.add(StrategieSperrkennzeichen(strategie_schluessel=str(strategy.id), gesetzt=True))
    await session.commit()

    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id, side="sell", size=D("0.01"))
    )

    assert ergebnis.eintrag.result == "versandt"
    assert ergebnis.eintrag.firmwide_lock_state == "gesetzt"
    assert ergebnis.eintrag.strategy_lock_state == "gesetzt"


# --- Verdrahtung mit dem echten Versand-Engpass (ohne B-6) -------------------------


async def test_mit_echtem_versandengpass_laeuft_bis_an_die_b6_wand(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    """Bis B-6 (OKX-Orderpfad) existiert, kann -- wie bei jedem anderen HED-36-Kind --
    selbst eine bestandene Prüfkette keine Order wirklich senden (HED-42)."""
    from app.trading.stufe2 import baue_versandengpass

    await _setze_position(session, portfolio.id, btc, "0.01")
    engpass = baue_versandengpass(market)

    with pytest.raises(NotImplementedError):
        await fuehre_automatischen_ausstieg_aus(
            session, market, engpass, _signal(strategy.id, side="sell", size=D("0.01"))
        )
