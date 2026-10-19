"""Fill-Erkennung (Z10 -> Z11/Z12/Z13) und Pflicht-Stop-Platzierung (B-12, HED-84,
Entwurf Abschnitt 6/7/9). Prüfplan P-2, P-5, P-6, P-9.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.dbtypes import utcnow
from app.models import Portfolio, Position, Strategy
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.models.proposals import (
    STATE_BOARD_FREIGEGEBEN,
    STATE_GEFUELLT,
    STATE_STUFE1_LAEUFT,
    STATE_STUFE2_LAEUFT,
    STATE_TEILGEFUELLT_BEENDET,
    STATE_VERSANDT,
    STATE_VON_GEGENSTELLE_ABGELEHNT,
    STATE_WARTET_AUF_BOARD,
)
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.models.stop_order import StopOrderBuch
from app.models.versandengpass import VersandEngpassEintrag
from app.sperrkennzeichen import ist_firmenweit_gesperrt
from app.trading.papier.okx_demo import OrderStatusErgebnis
from app.trading.proposals import Signal, create_proposal, transition
from app.trading.stop_pflicht import _pruefe_eskalation, pruefe_fills_einmal
from app.trading.versandengpass import Versandengpass
from tests.conftest import D, asset_id, create_portfolio

NACHFRIST = timedelta(seconds=120)


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


@pytest.fixture(autouse=True)
def papier_betriebsart(betriebsart):
    """``StopOrderBuch``/``StopAbgleichLauf`` tragen ``BetriebsartMixin`` (Auflage 1)
    -- jede Zeile braucht eine gesetzte Betriebsart."""
    betriebsart("papier")


@pytest.fixture(autouse=True)
def mit_schluessel(monkeypatch):
    """Startgatter (Auflage 2) verlangt einen lesbaren DEMO-Schlüssel, bevor
    ``Versandengpass``/``stop_pflicht`` überhaupt an die Gegenstelle gehen --
    auch wenn ``_FakeEngpass`` den eigentlichen Netzzugriff ersetzt (Vorbild:
    ``mit_schluessel`` in test_versand_treiber.py)."""
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


async def _versandter_vorschlag(
    session, strategy, *, direction="long", gegenstellen_order_id="okx-order-1"
):
    signal = Signal(
        signal_id="sig-1", strategy_id=strategy.id, instrument="BTC", direction=direction,
        size=D("0.5"), proposal_price=D(50000), stop_price=D(47500), proposal_kind="entry",
        signal_at=datetime.now(UTC),
    )
    proposal = await create_proposal(session, signal, mode="paper")
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice", board_action="approve"
    )
    await transition(session, proposal, STATE_STUFE2_LAEUFT, actor="system")
    await transition(session, proposal, STATE_VERSANDT, actor="system")
    proposal.limit_price = D(50000)
    proposal.gegenstellen_order_id = gegenstellen_order_id
    await session.commit()
    return proposal


class _FakeEngpass(Versandengpass):
    """Ersetzt die zwei Gegenstellen-Berührungspunkte (Lesevorgang + Senden) durch
    skriptierbare Fakes -- kein Netzzugriff, kein echter Schlüssel (Vorbild:
    ``_FakeEngpass`` in test_auto_exit.py)."""

    def __init__(self, *, stufe2_ok: bool = True):
        async def stufe2(_session, _order) -> bool:
            return stufe2_ok

        super().__init__(stufe2_pruefer=stufe2)
        self.status_antworten: dict[str, list] = {}
        self.sende_calls: list = []
        self._algo_zaehler = 0

    def plane_status(self, order_id: str, *antworten) -> None:
        self.status_antworten[order_id] = list(antworten)

    async def lese_order_status(self, exchange, order_id, symbol):
        warteschlange = self.status_antworten.get(order_id, [])
        if not warteschlange:
            return None
        return warteschlange.pop(0)

    async def _sende_an_gegenstelle(self, order) -> str:
        self.sende_calls.append(order)
        if order.gattung == "stornierung":
            return f"storno-von-{order.ziel_order_id}"
        self._algo_zaehler += 1
        return f"algo-{self._algo_zaehler}"


def _status(status: str, gefuellt, rest=Decimal(0)) -> OrderStatusErgebnis:
    return OrderStatusErgebnis(
        quelle="okx_demo", status=status, gefuellte_menge=D(gefuellt), restmenge=D(rest),
        zeitstempel=utcnow(),
    )


# --- P-2: alle fünf Fälle der Fill-Erkennung ----------------------------------------


async def test_vollstaendig_gefuellt_setzt_z11_und_platziert_stop(
    session, session_factory, market, prices, strategy, portfolio
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("gefuellt", "0.5"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_GEFUELLT
    assert proposal.gefuellte_menge == D("0.5")

    stop_order = next(o for o in engpass.sende_calls if o.gattung == "stop_platzierung")
    assert stop_order.quantity == D("0.5")  # P-5: Fill-Größe, nicht proposal.size
    assert stop_order.side == "sell"  # Gegenseite von long
    assert stop_order.stop_price == proposal.stop_price

    buch = await session.scalar(
        select(StopOrderBuch).where(StopOrderBuch.anlass_proposal_id == proposal.id)
    )
    assert buch is not None
    assert buch.zustand == "offen"
    assert buch.menge_soll == D("0.5")

    # HED-109: Fill-Buchung läuft vor der Stop-Platzierung -- Position/Cash müssen nach
    # diesem Durchlauf stimmen, nicht nur der Proposal-Zustand.
    btc_id = await asset_id(session, "BTC")
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc_id)
    )
    assert position is not None
    assert position.quantity == D("0.5")
    await session.refresh(portfolio)
    assert portfolio.cash < D("10000")  # Kauf hat Cash gekostet
    assert portfolio.fees_paid > D("0")
    assert portfolio.realized_pnl == D("0")  # nur Käufe über diese Pipeline (F-4)


async def test_teilgefuellt_und_von_gegenstelle_beendet_setzt_z12(
    session, session_factory, market, prices, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("beendet", "0.2"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_TEILGEFUELLT_BEENDET
    assert proposal.gefuellte_menge == D("0.2")
    stop_order = next(o for o in engpass.sende_calls if o.gattung == "stop_platzierung")
    assert stop_order.quantity == D("0.2")


async def test_abgelehnt_setzt_z13_ohne_stop(session, session_factory, market, strategy):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("abgelehnt", "0"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VON_GEGENSTELLE_ABGELEHNT
    assert not engpass.sende_calls


async def test_beendet_ohne_fuellung_zaehlt_wie_abgelehnt(
    session, session_factory, market, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("beendet", "0"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VON_GEGENSTELLE_ABGELEHNT


async def test_offen_bleibt_z10_ohne_stop(session, session_factory, market, strategy):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("offen", "0"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT
    assert not engpass.sende_calls


async def test_teilgefuellt_noch_aktiv_bleibt_z10_mit_stop_fuer_teilmenge(
    session, session_factory, market, prices, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("teilgefuellt", "0.3", "0.2"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT  # bleibt Z10
    assert proposal.gefuellte_menge == D("0.3")
    stop_order = next(o for o in engpass.sende_calls if o.gattung == "stop_platzierung")
    assert stop_order.quantity == D("0.3")


async def test_nicht_lesbar_bleibt_z10_kein_stiller_zwischenzustand(
    session, session_factory, market, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    # Keine geplante Antwort -> lese_order_status liefert None.

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT
    assert not engpass.sende_calls


# --- P-6: Nachführung bei Mehrfach-Teilfüllung, neu vor storno ----------------------


async def test_nachfuehrung_platziert_neuen_stop_vor_stornierung_des_alten(
    session, session_factory, market, prices, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("teilgefuellt", "0.3", "0.2"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)
    erster_stop = next(o for o in engpass.sende_calls if o.gattung == "stop_platzierung")
    assert erster_stop.quantity == D("0.3")

    # Zweiter Durchlauf: Rest füllt, Gesamtmenge jetzt 0.5.
    engpass.plane_status(proposal.gegenstellen_order_id, _status("gefuellt", "0.5"))
    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    gattungen = [o.gattung for o in engpass.sende_calls]
    # Reihenfolge ist die Entscheidung (F-13): neuer Stop zuerst, dann Stornierung.
    # Der Ersatz-Stop einer echten Nachführung läuft unter "stop_nachfuehrung", nicht
    # "stop_platzierung" (HED-192) -- nur diese Gattung hält ein gesetztes
    # Sperrkennzeichen laut Sperrwirkungstabelle (Entwurf 9.3) an.
    stop_index = gattungen.index("stop_nachfuehrung")
    storno_index = gattungen.index("stornierung")
    assert storno_index > stop_index

    buecher = list(
        await session.scalars(
            select(StopOrderBuch)
            .where(StopOrderBuch.anlass_proposal_id == proposal.id)
            .order_by(StopOrderBuch.id)
        )
    )
    assert len(buecher) == 2
    alter, neuer = buecher
    assert alter.zustand == "ersetzt"
    assert alter.ersetzt_durch_id == neuer.id
    assert neuer.zustand == "offen"
    assert neuer.menge_soll == D("0.5")


async def test_nachfuehrung_wird_vom_gesetzten_sperrkennzeichen_ueber_realen_erzeuger_angehalten(
    session, session_factory, market, prices, strategy
):
    """HED-192 (CRO-Abnahme HED-190, Entwurf 9.3/9.6 Pfad 6): belegt den realen
    Erzeuger (``_stelle_stop_sicher`` über ``pruefe_fills_einmal``), nicht nur die
    Sperrwirkungstabelle auf Versandengpass-Ebene (PR #40) -- echter ``Versandengpass``
    ohne ``_pruefe``-Override, nur Netzwerk/Status gefaked."""
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("teilgefuellt", "0.3", "0.2"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)
    assert any(o.gattung == "stop_platzierung" for o in engpass.sende_calls)

    # Sperrkennzeichen wird NACH der Erstplatzierung gesetzt -- genau der Fall, den
    # Pfad 6 prüft: eine laufende Position bekommt eine zweite Teilfüllung, während
    # der Kill-Switch aktiv ist.
    kennzeichen = await session.scalar(select(FirmenweitesSperrkennzeichen))
    kennzeichen.gesetzt = True
    await session.commit()

    vor_den_calls = len(engpass.sende_calls)
    engpass.plane_status(proposal.gegenstellen_order_id, _status("gefuellt", "0.5"))
    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    # Die Nachführung wird im echten Prüfschritt abgelehnt, bevor sie je an die
    # Gegenstelle geht -- kein neuer `_sende_an_gegenstelle`-Call für diesen Versuch.
    assert len(engpass.sende_calls) == vor_den_calls

    eintrag = await session.scalar(
        select(VersandEngpassEintrag)
        .where(VersandEngpassEintrag.gattung == "stop_nachfuehrung")
        .order_by(VersandEngpassEintrag.id.desc())
    )
    assert eintrag is not None
    assert eintrag.angenommen is False
    assert eintrag.ablehnungsgrund == "unterdrueckt_durch_sperre"

    # Das Buch bleibt beim alten, kleineren Stop -- keine stille zweite Zeile, keine
    # Stornierung des einzigen Schutzes, den die Position hat.
    buecher = list(
        await session.scalars(
            select(StopOrderBuch)
            .where(StopOrderBuch.anlass_proposal_id == proposal.id)
            .order_by(StopOrderBuch.id)
        )
    )
    assert len(buecher) == 1
    assert buecher[0].zustand == "offen"
    assert buecher[0].menge_soll == D("0.3")


# --- HED-109: Fill-Buchung Position/Cash (Entwurf Abschnitt 4, CRO-Abnahme HED-145) -


async def test_fill_buchung_mittelt_avg_cost_ueber_zwei_vorschlaege(
    session, session_factory, market, prices, strategy, portfolio
):
    """Zwei getrennte Vorschläge zu unterschiedlichen Preisen auf demselben
    Instrument/Portfolio -- die zweite Buchung muss ``avg_cost`` über beide Fills
    mitteln, nicht überschreiben (Testplan Entwurf Abschnitt 6, zweiter Punkt)."""
    erster = await _versandter_vorschlag(session, strategy, gegenstellen_order_id="okx-order-a")
    engpass = _FakeEngpass()
    engpass.plane_status(erster.gegenstellen_order_id, _status("gefuellt", "0.5"))
    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    zweiter = await _versandter_vorschlag(session, strategy, gegenstellen_order_id="okx-order-b")
    zweiter.limit_price = D(60000)
    await session.commit()
    engpass.plane_status(zweiter.gegenstellen_order_id, _status("gefuellt", "0.3"))
    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    btc_id = await asset_id(session, "BTC")
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc_id)
    )
    # Erster Fill: 0.5 @ 50000 USDC * 0,8 EUR/USD = 40000 EUR Kostenbasis.
    # Zweiter Fill: 0.3 @ 60000 USDC * 0,8 EUR/USD = 14400 EUR, gewichtet gemittelt:
    # (0,5*40000 + 14400) / 0,8 = 43000.
    assert position.quantity == D("0.8")
    assert position.avg_cost == D("43000.00")


async def test_fail_closed_ohne_portfolio_keine_buchung_kein_fortschritt(
    session, session_factory, market, strategy
):
    """Testplan Entwurf Abschnitt 6, Fail-closed-Fall: ``Strategy.portfolio_id`` fehlt
    -> keine Buchung, Proposal-Zustand unverändert, kein Stop (HED-109 Entwurf
    Abschnitt 4.2) -- derselbe Fail-closed-Grundsatz wie in ``_position_noch_offen``."""
    proposal = await _versandter_vorschlag(session, strategy)
    strategy.portfolio_id = None
    await session.commit()
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("gefuellt", "0.5"))

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT  # unverändert, kein stiller Teilerfolg
    assert proposal.gefuellte_menge is None
    assert not engpass.sende_calls  # kein Stop ohne Buchung


# --- P-9: Pflicht-Stop-Nachfrist -> Kill-Switch Stufe 2 -----------------------------


async def test_ueberschrittene_nachfrist_ohne_stop_setzt_firmenweites_kennzeichen(
    session, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    proposal.gefuellte_menge = D("0.5")
    proposal.filled_at = utcnow() - timedelta(seconds=200)
    await session.commit()

    await _pruefe_eskalation(session, proposal, NACHFRIST)
    await session.commit()

    assert await ist_firmenweit_gesperrt(session) is True
    zeilen = list(
        await session.scalars(
            select(SperrkennzeichenProtokoll).where(
                SperrkennzeichenProtokoll.ausloeser == "pflicht_stop_nicht_platziert"
            )
        )
    )
    assert len(zeilen) == 1


async def test_innerhalb_der_nachfrist_keine_eskalation(session, strategy):
    proposal = await _versandter_vorschlag(session, strategy)
    proposal.gefuellte_menge = D("0.5")
    proposal.filled_at = utcnow() - timedelta(seconds=10)
    await session.commit()

    await _pruefe_eskalation(session, proposal, NACHFRIST)
    await session.commit()

    assert await ist_firmenweit_gesperrt(session) is False


async def test_mit_offenem_stop_keine_eskalation_trotz_abgelaufener_nachfrist(
    session, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    proposal.gefuellte_menge = D("0.5")
    proposal.filled_at = utcnow() - timedelta(seconds=200)
    session.add(
        StopOrderBuch(
            strategie_schluessel=str(strategy.id), exchange="okx", symbol="BTC/USDC",
            side="sell", menge_soll=D("0.5"), trigger_soll=D(47500),
            gegenstellen_algo_id="algo-1", anlass_proposal_id=proposal.id, zustand="offen",
        )
    )
    await session.commit()

    await _pruefe_eskalation(session, proposal, NACHFRIST)
    await session.commit()

    assert await ist_firmenweit_gesperrt(session) is False


# --- E-12 (R-5, CRO-Nachforderung HED-107): Stop zu geschlossener Position --------


async def _gefuellter_vorschlag_mit_stop(
    session, session_factory, market, strategy, *, direction="long"
) -> tuple:
    """Bringt einen Vorschlag bis Z11 (vollständig gefüllt, Stop offen) -- gemeinsamer
    Aufsatzpunkt für die drei E-12-Fälle. Seit HED-109 bucht dieser Durchlauf den Fill
    bereits selbst in ``Position`` -- die Zeile existiert danach immer."""
    proposal = await _versandter_vorschlag(session, strategy, direction=direction)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("gefuellt", "0.5"))
    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)
    await session.refresh(proposal)
    assert proposal.state == STATE_GEFUELLT
    engpass.sende_calls.clear()  # nur die zweite Durchführung (E-12) ist der Testgegenstand
    return proposal, engpass


async def test_position_geschlossen_storniert_stop(
    session, session_factory, market, prices, strategy, portfolio
):
    """Auto-Exit hat die Position vollständig geschlossen (Position.quantity == 0) --
    der verwaiste Stop wird automatisch storniert, nicht erst vom nächsten täglichen
    Abgleich als Waise gemeldet (sonst unechter Stufe-2-Alarm, R-5).

    Die Exit-Buchung selbst ist ein eigener, noch offener Nebenbefund (HED-109 Entwurf
    Abschnitt 3) -- hier wird das Ergebnis einer Schließung (Menge 0) direkt auf der
    von der Einstiegsbuchung bereits angelegten Zeile simuliert, nicht über eine zweite
    Zeile (Unique Constraint portfolio_id/asset_id erlaubt nur eine)."""
    proposal, engpass = await _gefuellter_vorschlag_mit_stop(
        session, session_factory, market, strategy
    )
    btc_id = await asset_id(session, "BTC")
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc_id)
    )
    assert position is not None  # von der Einstiegsbuchung (HED-109) angelegt
    position.quantity = D("0")
    await session.commit()

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    storno_calls = [o for o in engpass.sende_calls if o.gattung == "stornierung"]
    assert len(storno_calls) == 1
    assert storno_calls[0].ziel_order_id is not None

    buch = await session.scalar(
        select(StopOrderBuch).where(StopOrderBuch.anlass_proposal_id == proposal.id)
    )
    await session.refresh(buch)
    assert buch.zustand == "storniert"
    assert buch.beendet_at is not None


async def test_position_noch_offen_kein_storno(
    session, session_factory, market, prices, strategy, portfolio
):
    """Position besteht unverändert fort (Auto-Exit hat nichts geschlossen) -- der
    Stop bleibt offen. Seit HED-109 legt bereits die Einstiegsbuchung die Zeile mit
    Menge 0.5 an, kein manuelles Anlegen mehr nötig."""
    proposal, engpass = await _gefuellter_vorschlag_mit_stop(
        session, session_factory, market, strategy
    )
    btc_id = await asset_id(session, "BTC")
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == btc_id)
    )
    assert position is not None
    assert position.quantity == D("0.5")

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    assert not any(o.gattung == "stornierung" for o in engpass.sende_calls)
    buch = await session.scalar(
        select(StopOrderBuch).where(StopOrderBuch.anlass_proposal_id == proposal.id)
    )
    await session.refresh(buch)
    assert buch.zustand == "offen"


async def test_keine_positionszeile_fail_closed_kein_storno(
    session, session_factory, market, strategy, portfolio
):
    """Fail-closed-Regressionswächter: eine fehlende Buchzeile ist „nicht
    entscheidbar", nicht „geschlossen" (``_position_noch_offen``). Seit HED-109 bucht
    eine frische Einstiegsfüllung über diese Pipeline die Zeile selbst -- der Fall
    "keine Zeile vorhanden" ist also nicht mehr über den normalen Fill-Pfad erreichbar
    und wird hier direkt konstruiert (z. B. eine Datenkorrektur könnte die Zeile
    entfernen, ohne den Stop abzuräumen). Zustand wird dafür direkt gesetzt, ohne über
    `pruefe_fills_einmal` zu laufen -- so ist `market`/Preiskonvertierung für diesen
    Teil nicht nötig."""
    proposal = await _versandter_vorschlag(session, strategy)
    proposal.state = STATE_GEFUELLT
    proposal.gefuellte_menge = D("0.5")
    proposal.filled_at = utcnow()
    session.add(
        StopOrderBuch(
            strategie_schluessel=str(strategy.id), exchange="okx", symbol="BTC/USDC",
            side="sell", menge_soll=D("0.5"), trigger_soll=D(47500),
            gegenstellen_algo_id="algo-1", anlass_proposal_id=proposal.id, zustand="offen",
        )
    )
    await session.commit()
    engpass = _FakeEngpass()
    # Bewusst keine Position-Zeile angelegt.

    await pruefe_fills_einmal(session_factory, engpass, market, pflicht_stop_nachfrist=NACHFRIST)

    assert not any(o.gattung == "stornierung" for o in engpass.sende_calls)
    buch = await session.scalar(
        select(StopOrderBuch).where(StopOrderBuch.anlass_proposal_id == proposal.id)
    )
    await session.refresh(buch)
    assert buch.zustand == "offen"
