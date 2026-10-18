"""Exit-Fill-Erkennung: Stop- und ``auto_exit``-Schließungen in Position/Cash (HED-147,
Entwurf Abschnitt 4.2, Testplan Abschnitt 6, CRO-Abnahme HED-148/HED-150).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.dbtypes import utcnow
from app.models import Portfolio, Position, Strategy
from app.models.auto_exit import AutoExitLogEntry
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.models.stop_order import StopOrderBuch
from app.trading.exit_fill_erkennung import pruefe_exit_fills_einmal
from app.trading.papier.okx_demo import OrderStatusErgebnis
from app.trading.versandengpass import Versandengpass
from tests.conftest import D, asset_id, create_portfolio


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


@pytest.fixture(autouse=True)
def papier_betriebsart(betriebsart):
    betriebsart("papier")


@pytest.fixture(autouse=True)
def mit_schluessel(monkeypatch):
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
async def btc(session, prices) -> int:
    return await asset_id(session, "BTC")


async def _position(session, portfolio_id: int, asset_id_: int, quantity, avg_cost="50000"):
    session.add(
        Position(
            portfolio_id=portfolio_id,
            asset_id=asset_id_,
            quantity=D(quantity),
            avg_cost=D(avg_cost),
        )
    )
    await session.flush()


async def _stop_zeile(session, strategy, **overrides) -> StopOrderBuch:
    felder = dict(
        strategie_schluessel=str(strategy.id),
        exchange="okx",
        symbol="BTC/USDC",
        side="sell",
        menge_soll=D("0.5"),
        trigger_soll=D("47500"),
        gegenstellen_algo_id="algo-1",
        zustand="offen",
    )
    felder.update(overrides)
    zeile = StopOrderBuch(**felder)
    session.add(zeile)
    await session.flush()
    return zeile


async def _auto_exit_zeile(session, strategy, **overrides) -> AutoExitLogEntry:
    felder = dict(
        mode="paper",
        strategy_id=strategy.id,
        trigger_signal_id="exit-sig-1",
        trigger_signal_kind="strategie_ausstieg",
        trigger_at=datetime.now(UTC),
        sent_at=datetime.now(UTC),
        filled_at=None,
        instrument="BTC",
        exit_size=D("0.5"),
        position_before=D("0.5"),
        position_after=D("0"),
        order_direction="verkauf",
        position_direction_closed="long",
        price_age_seconds=0,
        price_source_timestamp=datetime.now(UTC),
        size_was_capped=False,
        size_capped_to=None,
        is_reversal=False,
        reversal_counter_proposal_id=None,
        firmwide_lock_state="offen",
        strategy_lock_state="offen",
        result="versandt",
        escalation_at=None,
        escalation_recipients=None,
        escalation_deadline=None,
        escalation_result=None,
        correction_of_id=None,
        gegenstellen_order_id="order-1",
        exchange="okx",
        boersensymbol="BTC/USDC",
        gefuellte_menge_gebucht=D("0"),
    )
    felder.update(overrides)
    zeile = AutoExitLogEntry(**felder)
    session.add(zeile)
    await session.flush()
    return zeile


class _FakeEngpass(Versandengpass):
    """Ersetzt den Lesevorgang durch skriptierbare Fakes -- kein Netzzugriff.
    Vorbild: ``_FakeEngpass`` in test_stop_pflicht.py, hier erweitert um
    ``ist_algo_order``, weil Teilaufgabe A (Stop) und Teilaufgabe B (auto_exit)
    getrennte Lookups über denselben Order-Id-Namensraum vermeiden müssen."""

    def __init__(self):
        super().__init__()
        self.status_antworten: dict[tuple[str, bool], list] = {}

    def plane_status(self, order_id: str, *antworten, ist_algo_order: bool = False) -> None:
        self.status_antworten[(order_id, ist_algo_order)] = list(antworten)

    async def lese_order_status(self, exchange, order_id, symbol, *, ist_algo_order=False):
        warteschlange = self.status_antworten.get((order_id, ist_algo_order), [])
        if not warteschlange:
            return None
        return warteschlange.pop(0)


def _status(status: str, gefuellt, rest=Decimal(0), preis=None) -> OrderStatusErgebnis:
    return OrderStatusErgebnis(
        quelle="okx_demo", status=status, gefuellte_menge=D(gefuellt), restmenge=D(rest),
        zeitstempel=utcnow(), durchschnittspreis=D(preis) if preis is not None else None,
    )


# --- Teilaufgabe A: Stop-Order -------------------------------------------------------


async def test_stop_vollstaendig_gefuellt_bucht_und_setzt_ausgeloest(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    stop = await _stop_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status(
        "algo-1", _status("gefuellt", "0.5", preis="49000"), ist_algo_order=True
    )

    bearbeitet = await pruefe_exit_fills_einmal(session_factory, engpass, market)

    assert bearbeitet == 1
    await session.refresh(stop)
    assert stop.zustand == "ausgeloest"
    assert stop.beendet_at is not None
    assert stop.gefuellte_menge_gebucht == D("0.5")

    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    # Festlegung N-3: Exit-Aufrufer setzen zeile_behalten=True -- die Zeile bleibt mit
    # Menge 0 bestehen, sonst würde _position_noch_offen (stop_pflicht.py) eine
    # fehlende Zeile fälschlich als "noch offen" lesen.
    assert position is not None
    assert position.quantity == D("0")
    await session.refresh(portfolio)
    assert portfolio.cash > D("10000")  # Verkaufserlös gutgeschrieben
    assert portfolio.fees_paid > D("0")
    assert portfolio.realized_pnl != D("0")


async def test_stop_teilfuellung_ueber_zwei_durchlaeufe_summiert_korrekt(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "1.0")
    stop = await _stop_zeile(session, strategy, menge_soll=D("1.0"))
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status(
        "algo-1",
        _status("teilgefuellt", "0.4", "0.6", preis="49000"),
        _status("gefuellt", "1.0", preis="48900"),
        ist_algo_order=True,
    )

    await pruefe_exit_fills_einmal(session_factory, engpass, market)
    await session.refresh(stop)
    assert stop.zustand == "offen"  # Tabelle 4.5: teilgefuellt -> unverändert
    assert stop.gefuellte_menge_gebucht == D("0.4")
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position.quantity == D("0.6")

    await pruefe_exit_fills_einmal(session_factory, engpass, market)
    await session.refresh(stop)
    assert stop.zustand == "ausgeloest"
    assert stop.gefuellte_menge_gebucht == D("1.0")
    # session's identity map hält die zuvor geladene Position-Zeile mit
    # expire_on_commit=False -- der zweite Durchlauf hat über eine andere Session
    # committet, also muss hier explizit neu gelesen werden.
    await session.refresh(position)
    assert position.quantity == D("0")


async def test_stop_idempotent_bei_wiederholtem_gleichen_status(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    """Ohne gefuellte_menge_gebucht würde jeder Durchlauf erneut buchen -- dieser Test
    muss ohne das Delta-Feld fehlschlagen."""
    await _position(session, portfolio.id, btc, "0.5")
    await _stop_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    status = _status("gefuellt", "0.5", preis="49000")
    engpass.plane_status("algo-1", status, status, status, ist_algo_order=True)

    for _ in range(3):
        await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(portfolio)
    erwartete_realized = portfolio.realized_pnl
    # Eine zweite Runde mit erneut demselben Status darf nichts mehr verändern.
    engpass.plane_status("algo-1", status, ist_algo_order=True)
    await pruefe_exit_fills_einmal(session_factory, engpass, market)
    await session.refresh(portfolio)
    assert portfolio.realized_pnl == erwartete_realized


async def test_stop_fail_closed_ohne_position_bucht_nichts(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    """Festlegung N-2: keine Position-Zeile -> nicht buchen, Zustand unverändert."""
    stop = await _stop_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status(
        "algo-1", _status("gefuellt", "0.5", preis="49000"), ist_algo_order=True
    )

    bearbeitet = await pruefe_exit_fills_einmal(session_factory, engpass, market)

    assert bearbeitet == 0
    await session.refresh(stop)
    assert stop.zustand == "offen"
    assert stop.gefuellte_menge_gebucht in (D("0"), None)
    await session.refresh(portfolio)
    assert portfolio.cash == D("10000")


async def test_stop_fail_closed_position_zu_klein_bucht_nichts(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    """Festlegung N-2: gemeldetes Delta groesser als position.quantity -> nicht
    buchen, keine negative Position."""
    await _position(session, portfolio.id, btc, "0.2")
    stop = await _stop_zeile(session, strategy, menge_soll=D("0.5"))
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status(
        "algo-1", _status("gefuellt", "0.5", preis="49000"), ist_algo_order=True
    )

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(stop)
    assert stop.zustand == "offen"
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position.quantity == D("0.2")  # unverändert, nie negativ


async def test_stop_fail_closed_ohne_preis_bucht_nichts(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    """Festlegung N-10: kein positiver Fill-Preis -> nicht buchen, Zustand
    unverändert."""
    await _position(session, portfolio.id, btc, "0.5")
    stop = await _stop_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status(
        "algo-1", _status("gefuellt", "0.5", preis=None), ist_algo_order=True
    )

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(stop)
    assert stop.zustand == "offen"
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position.quantity == D("0.5")  # unverändert


async def test_stop_nicht_lesbar_bleibt_unveraendert(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    stop = await _stop_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()  # keine geplante Antwort -> lese_order_status liefert None

    bearbeitet = await pruefe_exit_fills_einmal(session_factory, engpass, market)

    assert bearbeitet == 0
    await session.refresh(stop)
    assert stop.zustand == "offen"


async def test_stop_beendet_ohne_fuellung_setzt_storniert(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    stop = await _stop_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status("algo-1", _status("beendet", "0"), ist_algo_order=True)

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(stop)
    assert stop.zustand == "storniert"
    assert stop.beendet_at is not None


# --- Teilaufgabe B: auto_exit ---------------------------------------------------------


async def test_auto_exit_vollstaendig_gefuellt_bucht_und_setzt_gefuellt(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    eintrag = await _auto_exit_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status("order-1", _status("gefuellt", "0.5", preis="49500"))

    bearbeitet = await pruefe_exit_fills_einmal(session_factory, engpass, market)

    assert bearbeitet == 1
    await session.refresh(eintrag)
    assert eintrag.result == "gefuellt"
    assert eintrag.filled_at is not None
    assert eintrag.position_after == D("0")
    assert eintrag.gefuellte_menge_gebucht == D("0.5")

    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position is not None
    assert position.quantity == D("0")
    await session.refresh(portfolio)
    assert portfolio.cash > D("10000")


async def test_auto_exit_teilfuellung_setzt_teilgefuellt_bei_beendet(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    eintrag = await _auto_exit_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status("order-1", _status("beendet", "0.2", preis="49500"))

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(eintrag)
    assert eintrag.result == "teilgefuellt"
    assert eintrag.filled_at is not None
    assert eintrag.gefuellte_menge_gebucht == D("0.2")
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position.quantity == D("0.3")


async def test_auto_exit_teilgefuellt_bleibt_versandt_mit_filled_at(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    eintrag = await _auto_exit_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status("order-1", _status("teilgefuellt", "0.2", "0.3", preis="49500"))

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(eintrag)
    assert eintrag.result == "versandt"  # Tabelle 4.5: teilgefuellt -> unverändert
    assert eintrag.filled_at is not None  # beim ersten Fill gesetzt (Festlegung N-12)
    assert eintrag.gefuellte_menge_gebucht == D("0.2")


async def test_auto_exit_abgelehnt_bucht_nichts(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    eintrag = await _auto_exit_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status("order-1", _status("abgelehnt", "0"))

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(eintrag)
    assert eintrag.result == "von_gegenstelle_abgelehnt"
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position.quantity == D("0.5")  # unverändert


async def test_auto_exit_historische_zeile_ohne_order_id_wird_uebersprungen(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    """Festlegung N-6: keine Nachbuchung historischer Exits."""
    eintrag = await _auto_exit_zeile(session, strategy, gegenstellen_order_id=None)
    await session.commit()
    engpass = _FakeEngpass()

    bearbeitet = await pruefe_exit_fills_einmal(session_factory, engpass, market)

    assert bearbeitet == 0
    await session.refresh(eintrag)
    assert eintrag.result == "versandt"


async def test_auto_exit_fail_closed_ohne_preis_bucht_nichts(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    await _position(session, portfolio.id, btc, "0.5")
    eintrag = await _auto_exit_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status("order-1", _status("gefuellt", "0.5", preis=None))

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(eintrag)
    assert eintrag.result == "versandt"
    assert eintrag.filled_at is None
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position.quantity == D("0.5")


# --- Regressionstest, reproduziert genau den Nebenbefund (Testplan Punkt 15/16) ----


async def test_regression_stop_fill_schliesst_position_in_nav(
    session, session_factory, market, prices, strategy, portfolio, btc
):
    """Vor dem Fix (ohne dieses Modul): die Position bliebe in voller Größe im Buch,
    NAV enthielte weiterhin die geschlossene Position, realized_pnl wäre 0. Dieser
    Test belegt den Fix end-to-end über den öffentlichen Einstiegspunkt."""
    await _position(session, portfolio.id, btc, "0.5")
    stop = await _stop_zeile(session, strategy)
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status(
        "algo-1", _status("gefuellt", "0.5", preis="49000"), ist_algo_order=True
    )

    await pruefe_exit_fills_einmal(session_factory, engpass, market)

    await session.refresh(stop)
    await session.refresh(portfolio)
    assert stop.zustand == "ausgeloest"
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc)
    )
    assert position is not None
    assert position.quantity == D("0")
    assert portfolio.realized_pnl != D("0")
