"""Fill-Erkennung (Z10 -> Z11/Z12/Z13) und Pflicht-Stop-Platzierung (B-12, HED-84,
Entwurf Abschnitt 6/7/9). Prüfplan P-2, P-5, P-6, P-9.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.dbtypes import utcnow
from app.models import Portfolio, Strategy
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
from app.sperrkennzeichen import ist_firmenweit_gesperrt
from app.trading.papier.okx_demo import OrderStatusErgebnis
from app.trading.proposals import Signal, create_proposal, transition
from app.trading.stop_pflicht import _pruefe_eskalation, pruefe_fills_einmal
from app.trading.versandengpass import Versandengpass
from tests.conftest import D, create_portfolio

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
    session, session_factory, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("gefuellt", "0.5"))

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

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


async def test_teilgefuellt_und_von_gegenstelle_beendet_setzt_z12(
    session, session_factory, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("beendet", "0.2"))

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_TEILGEFUELLT_BEENDET
    assert proposal.gefuellte_menge == D("0.2")
    stop_order = next(o for o in engpass.sende_calls if o.gattung == "stop_platzierung")
    assert stop_order.quantity == D("0.2")


async def test_abgelehnt_setzt_z13_ohne_stop(session, session_factory, strategy):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("abgelehnt", "0"))

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VON_GEGENSTELLE_ABGELEHNT
    assert not engpass.sende_calls


async def test_beendet_ohne_fuellung_zaehlt_wie_abgelehnt(session, session_factory, strategy):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("beendet", "0"))

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VON_GEGENSTELLE_ABGELEHNT


async def test_offen_bleibt_z10_ohne_stop(session, session_factory, strategy):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("offen", "0"))

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT
    assert not engpass.sende_calls


async def test_teilgefuellt_noch_aktiv_bleibt_z10_mit_stop_fuer_teilmenge(
    session, session_factory, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("teilgefuellt", "0.3", "0.2"))

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT  # bleibt Z10
    assert proposal.gefuellte_menge == D("0.3")
    stop_order = next(o for o in engpass.sende_calls if o.gattung == "stop_platzierung")
    assert stop_order.quantity == D("0.3")


async def test_nicht_lesbar_bleibt_z10_kein_stiller_zwischenzustand(
    session, session_factory, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    # Keine geplante Antwort -> lese_order_status liefert None.

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

    await session.refresh(proposal)
    assert proposal.state == STATE_VERSANDT
    assert not engpass.sende_calls


# --- P-6: Nachführung bei Mehrfach-Teilfüllung, neu vor storno ----------------------


async def test_nachfuehrung_platziert_neuen_stop_vor_stornierung_des_alten(
    session, session_factory, strategy
):
    proposal = await _versandter_vorschlag(session, strategy)
    engpass = _FakeEngpass()
    engpass.plane_status(proposal.gegenstellen_order_id, _status("teilgefuellt", "0.3", "0.2"))

    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)
    erster_stop = next(o for o in engpass.sende_calls if o.gattung == "stop_platzierung")
    assert erster_stop.quantity == D("0.3")

    # Zweiter Durchlauf: Rest füllt, Gesamtmenge jetzt 0.5.
    engpass.plane_status(proposal.gegenstellen_order_id, _status("gefuellt", "0.5"))
    await pruefe_fills_einmal(session_factory, engpass, pflicht_stop_nachfrist=NACHFRIST)

    gattungen = [o.gattung for o in engpass.sende_calls]
    # Reihenfolge ist die Entscheidung (F-13): neuer Stop zuerst, dann Stornierung.
    stop_index = gattungen.index("stop_platzierung", gattungen.index("stop_platzierung") + 1)
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
