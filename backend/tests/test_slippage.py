"""Zweiphasige Slippage-Messung (Entwurf HED-36 Fassung 1.1 Abschnitt 11, HED-49).

Phase 1 (Signal -> Freigabe) und Phase 2 (Freigabe -> Füllung) werden getrennt
geprüft, einschließlich der ausdrücklichen Festlegung 11.2: es gibt keine Funktion und
kein Feld, das beide zu einer Gesamtzahl vermischt.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import SlippageMessungEintrag, Strategy
from app.models.proposals import (
    STATE_BOARD_FREIGEGEBEN,
    STATE_ENTSTANDEN,
    STATE_GEFUELLT,
    STATE_STUFE1_LAEUFT,
    STATE_STUFE2_LAEUFT,
    STATE_TEILGEFUELLT_BEENDET,
    STATE_VERSANDT,
    STATE_VON_GEGENSTELLE_ABGELEHNT,
    STATE_WARTET_AUF_BOARD,
    Proposal,
)
from app.slippage import (
    MessungNichtMoeglich,
    berechne_fuellquote,
    berechne_phase1,
    berechne_phase2,
    erfasse_messung,
)
from app.trading.proposals import Signal, create_proposal, transition

SIGNAL_AT = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


async def _strategy(session, gate_status: str = "S3") -> Strategy:
    strategy = Strategy(gate_status=gate_status)
    session.add(strategy)
    await session.flush()
    return strategy


async def _proposal(
    session,
    *,
    direction: str = "long",
    proposal_price=Decimal("100"),
    stop_price=Decimal("90"),
    signal_id: str = "sig-1",
) -> Proposal:
    strategy = await _strategy(session)
    signal = Signal(
        signal_id=signal_id,
        strategy_id=strategy.id,
        instrument="BTC/USDT",
        direction=direction,
        size=Decimal("1"),
        proposal_price=proposal_price,
        stop_price=stop_price,
        proposal_kind="entry",
        signal_at=SIGNAL_AT,
    )
    proposal = await create_proposal(session, signal, mode="papier")
    await session.commit()
    return proposal


async def _bis_wartet_auf_board(session, proposal: Proposal) -> Proposal:
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await session.commit()
    return proposal


async def _freigegeben(session, proposal: Proposal, *, limit_price: Decimal) -> Proposal:
    """Entscheidungs-Zeitpunkt setzen (HED-41/46) und die -- hier noch nicht
    automatisch verdrahtete, siehe app/slippage.py -- Dashboard-Referenzkurs-Angabe."""
    await _bis_wartet_auf_board(session, proposal)
    await transition(session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice")
    proposal.limit_price = limit_price
    await session.commit()
    return proposal


async def _gefuellt(
    session,
    proposal: Proposal,
    *,
    limit_price: Decimal,
    sent_at,
    filled_at,
    zielzustand: str = STATE_GEFUELLT,
) -> Proposal:
    await _freigegeben(session, proposal, limit_price=limit_price)
    await transition(session, proposal, STATE_STUFE2_LAEUFT, actor="system")
    await transition(session, proposal, STATE_VERSANDT, actor="system")
    proposal.sent_at = sent_at
    await transition(session, proposal, zielzustand, actor="system")
    proposal.filled_at = filled_at
    await session.commit()
    return proposal


# --- Phase 1 --------------------------------------------------------------------


async def test_phase1_dauer_und_kursveraenderung(session, betriebsart):
    betriebsart("papier")
    proposal = await _proposal(session)
    await _freigegeben(session, proposal, limit_price=Decimal("103"))

    phase1 = berechne_phase1(proposal)

    assert phase1.dauer_sekunden == pytest.approx(
        (proposal.decided_at - SIGNAL_AT).total_seconds(), abs=2
    )
    assert phase1.kursveraenderung == Decimal("3")  # 103 - 100


async def test_phase1_ohne_limit_price_ist_kursveraenderung_none(session, betriebsart):
    """Noch nicht vom Dashboard gesetzt (HED-44-Folgeissue) -- informativ fehlend, kein
    Fehler, anders als bei Phase 2."""
    betriebsart("papier")
    proposal = await _proposal(session)
    await _bis_wartet_auf_board(session, proposal)
    await transition(session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice")
    await session.commit()

    phase1 = berechne_phase1(proposal)

    assert phase1.kursveraenderung is None


async def test_phase1_ohne_entscheidung_schlaegt_fehl(session, betriebsart):
    betriebsart("papier")
    proposal = await _proposal(session)
    await _bis_wartet_auf_board(session, proposal)

    with pytest.raises(MessungNichtMoeglich):
        berechne_phase1(proposal)


# --- Phase 2 --------------------------------------------------------------------


async def test_phase2_long_teureres_fuellen_ist_ungueenstig(session, betriebsart):
    betriebsart("papier")
    decided = datetime(2026, 10, 10, 12, 0, 10, tzinfo=UTC)
    sent = decided + timedelta(seconds=2)
    filled = sent + timedelta(seconds=3)
    proposal = await _gefuellt(
        session,
        await _proposal(session),
        limit_price=Decimal("100"),
        sent_at=sent,
        filled_at=filled,
    )
    proposal.decided_at = decided  # fester Wert für die Dauer-Assertion unten

    phase2 = berechne_phase2(proposal, Decimal("101"))

    assert phase2.dauer_freigabe_bis_versand_sekunden == 2
    assert phase2.dauer_versand_bis_fuellung_sekunden == 3
    assert phase2.dauer_freigabe_bis_fuellung_sekunden == 5
    assert phase2.preisabweichung_absolut == Decimal("1")  # 101 - 100, teurer = ungünstig
    assert phase2.preisabweichung_prozentpunkte == Decimal("1")  # 1/100 * 100
    assert phase2.preisabweichung_risikoeinheiten == Decimal("0.1")  # 1 / stop_distance(10)


async def test_phase2_short_vorzeichen_gedreht(session, betriebsart):
    """Short: billiger verkauft = ungünstig = positiver Wert (Entwurf 11.4)."""
    betriebsart("papier")
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    kurzposition = await _proposal(
        session, direction="short", proposal_price=Decimal("100"), stop_price=Decimal("110")
    )
    proposal = await _gefuellt(
        session, kurzposition, limit_price=Decimal("100"), sent_at=now, filled_at=now
    )

    # teurer zurückgekauft = günstig für Short
    guenstiger = berechne_phase2(proposal, Decimal("101"))
    # billiger verkauft = ungünstig
    unguenstiger = berechne_phase2(proposal, Decimal("99"))

    assert guenstiger.preisabweichung_absolut == Decimal("-1")
    assert unguenstiger.preisabweichung_absolut == Decimal("1")


@pytest.mark.parametrize(
    "fehlendes_feld", ["decided_at", "sent_at", "filled_at", "limit_price"]
)
async def test_phase2_ohne_zeitstempel_oder_referenzkurs_schlaegt_fehl(
    session, betriebsart, fehlendes_feld
):
    betriebsart("papier")
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    proposal = await _gefuellt(
        session, await _proposal(session), limit_price=Decimal("100"), sent_at=now, filled_at=now
    )
    setattr(proposal, fehlendes_feld, None)

    with pytest.raises(MessungNichtMoeglich, match=fehlendes_feld):
        berechne_phase2(proposal, Decimal("101"))


async def test_keine_gesamtkennzahl_existiert():
    """Festlegung 11.2: keine zusammengefasste Gesamt-Slippage, auch nicht als Teil
    einer anders benannten Funktion oder eines Felds."""
    import app.slippage as modul

    verdaechtig = [name for name in dir(modul) if "gesamt" in name.lower()]
    assert verdaechtig == []


# --- Persistenz (erfasse_messung) -----------------------------------------------


async def test_erfasse_messung_schreibt_eine_unveraenderliche_zeile(session, betriebsart):
    betriebsart("papier")
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    proposal = await _gefuellt(
        session, await _proposal(session), limit_price=Decimal("100"), sent_at=now, filled_at=now
    )

    eintrag = await erfasse_messung(session, proposal, fuellpreis=Decimal("102"))

    assert eintrag.proposal_id == proposal.id
    assert eintrag.fuellpreis == Decimal("102")
    assert eintrag.preisabweichung_absolut == Decimal("2")
    gespeichert = await session.scalar(
        select(SlippageMessungEintrag).where(SlippageMessungEintrag.proposal_id == proposal.id)
    )
    assert gespeichert is not None
    assert gespeichert.betriebsart == "papier"


async def test_erfasse_messung_zweimal_schlaegt_an_unique_constraint_fehl(session, betriebsart):
    betriebsart("papier")
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    proposal = await _gefuellt(
        session, await _proposal(session), limit_price=Decimal("100"), sent_at=now, filled_at=now
    )
    await erfasse_messung(session, proposal, fuellpreis=Decimal("102"))

    with pytest.raises(IntegrityError):
        await erfasse_messung(session, proposal, fuellpreis=Decimal("102"))
    await session.rollback()


@pytest.mark.parametrize("zielzustand", [STATE_VERSANDT, STATE_WARTET_AUF_BOARD, STATE_ENTSTANDEN])
async def test_erfasse_messung_ohne_fuellung_schlaegt_fehl(session, betriebsart, zielzustand):
    betriebsart("papier")
    proposal = await _proposal(session)
    if zielzustand != STATE_ENTSTANDEN:
        await _bis_wartet_auf_board(session, proposal)
    if zielzustand == STATE_VERSANDT:
        proposal.limit_price = Decimal("100")
        await transition(session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice")
        await transition(session, proposal, STATE_STUFE2_LAEUFT, actor="system")
        await transition(session, proposal, STATE_VERSANDT, actor="system")
        await session.commit()

    with pytest.raises(MessungNichtMoeglich):
        await erfasse_messung(session, proposal, fuellpreis=Decimal("100"))


async def test_erfasse_messung_bei_teilgefuellt_beendet(session, betriebsart):
    """Teilfüllung ist ein gültiger Endzustand für die Messung (Tabelle 4.2, Z12)."""
    betriebsart("papier")
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    proposal = await _gefuellt(
        session,
        await _proposal(session),
        limit_price=Decimal("100"),
        sent_at=now,
        filled_at=now,
        zielzustand=STATE_TEILGEFUELLT_BEENDET,
    )

    eintrag = await erfasse_messung(session, proposal, fuellpreis=Decimal("100.5"))

    assert eintrag.preisabweichung_absolut == Decimal("0.5")


# --- Füllquote -------------------------------------------------------------------


async def test_fuellquote_zaehlt_nur_abgeschlossene_versandte_vorschlaege(session, betriebsart):
    betriebsart("papier")
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    gefuellt_1 = await _gefuellt(
        session,
        await _proposal(session, signal_id="a"),
        limit_price=Decimal("100"),
        sent_at=now,
        filled_at=now,
    )
    gefuellt_2 = await _gefuellt(
        session,
        await _proposal(session, signal_id="b"),
        limit_price=Decimal("100"),
        sent_at=now,
        filled_at=now,
    )
    teilgefuellt = await _gefuellt(
        session,
        await _proposal(session, signal_id="c"),
        limit_price=Decimal("100"),
        sent_at=now,
        filled_at=now,
        zielzustand=STATE_TEILGEFUELLT_BEENDET,
    )
    abgelehnt_von_gegenstelle = await _gefuellt(
        session,
        await _proposal(session, signal_id="d"),
        limit_price=Decimal("100"),
        sent_at=now,
        filled_at=now,
        zielzustand=STATE_VON_GEGENSTELLE_ABGELEHNT,
    )
    noch_offen = await _proposal(session, signal_id="e")  # Zustand entstanden, zählt nicht

    quote = berechne_fuellquote(
        [gefuellt_1, gefuellt_2, teilgefuellt, abgelehnt_von_gegenstelle, noch_offen]
    )

    assert quote == Decimal("2") / Decimal("4")  # 2 von 4 abgeschlossenen, "noch_offen" fehlt


async def test_fuellquote_ohne_kohorte_schlaegt_fehl(session, betriebsart):
    betriebsart("papier")
    proposal = await _proposal(session)

    with pytest.raises(MessungNichtMoeglich):
        berechne_fuellquote([proposal])
