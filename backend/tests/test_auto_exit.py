"""Automatischer strategiegetriebener Ausstieg (Entwurf HED-36 Abschnitt 10,
CRO-freigegeben). Testpfade 22 (Reversal-Aufspaltung) und 23 (eskaliert_kursfrische)
aus Abschnitt 9.6, plus A1-A6/B1-B3 der Prüfkette aus Abschnitt 10.6.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Portfolio, Position, Strategy
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen
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


async def test_ohne_offene_position_kein_automatischer_ausstieg(
    mit_schluessel, session, market, prices, strategy
):
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    ergebnis = await fuehre_automatischen_ausstieg_aus(
        session, market, engpass, _signal(strategy.id)
    )
    assert ergebnis.eintrag.result == "abgelehnt_groesse"
    assert ergebnis.eintrag.is_reversal is False


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


async def test_reversal_fehlt_preis_eskaliert_nicht_sondern_wirft(
    mit_schluessel, session, market, prices, strategy, portfolio, btc
):
    await _setze_position(session, portfolio.id, btc, "0.03")
    engpass = _FakeEngpass(stufe2_pruefer=_StufePruefer(True))
    signal = _signal(strategy.id, side="sell", gegenposition_size=D("0.05"))
    with pytest.raises(AutoExitFehler):
        await fuehre_automatischen_ausstieg_aus(session, market, engpass, signal)


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
