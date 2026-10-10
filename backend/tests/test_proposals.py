"""Vorschlag-Datenmodell, Zustandsautomat, Gate-Weiche.

Deckt die 9 Testfälle aus HED-41 / HED-36 Entwurf Fassung 1.1 Abschnitt 4 (CRO-freigegeben,
siehe Dokument "spezifikation" auf HED-41) sowie die Unveränderlichkeits-Invariante aus
Festlegung 4.4.6 ab.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.models import Base, ProposalEvent, Strategy
from app.models.proposals import (
    STATE_BOARD_GEAENDERT,
    STATE_ENTSTANDEN,
    STATE_STUFE1_ABGELEHNT,
    STATE_STUFE1_LAEUFT,
    STATE_VERFALLEN,
    STATE_WARTET_AUF_BOARD,
)
from app.trading.proposals import (
    InvalidTransition,
    Signal,
    board_change,
    create_proposal,
    transition,
)


def _url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'proposals.db').as_posix()}"


@pytest.fixture
async def session(tmp_path):
    engine = make_engine(_url(tmp_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as db:
        yield db
    await engine.dispose()


async def make_strategy(session, gate_status: str) -> Strategy:
    strategy = Strategy(gate_status=gate_status)
    session.add(strategy)
    await session.flush()
    return strategy


def make_signal(strategy_id: int, signal_id: str = "sig-1") -> Signal:
    return Signal(
        signal_id=signal_id,
        strategy_id=strategy_id,
        instrument="BTC/USDT",
        direction="long",
        size=Decimal("1"),
        proposal_price=Decimal("100"),
        stop_price=Decimal("95"),
        proposal_kind="entry",
        signal_at=datetime.now(UTC),
    )


async def events_of(session, proposal_id: int) -> list[ProposalEvent]:
    result = await session.scalars(
        select(ProposalEvent)
        .where(ProposalEvent.proposal_id == proposal_id)
        .order_by(ProposalEvent.id)
    )
    return list(result)


# --- Gate-Weiche (Testfälle 1-3, 5) -----------------------------------------------------


async def test_signal_at_gate_s1_creates_no_proposal(session):
    strategy = await make_strategy(session, "S1")
    result = await create_proposal(session, make_signal(strategy.id), mode="paper")
    assert result is None


async def test_signal_at_gate_s2_creates_no_proposal(session):
    strategy = await make_strategy(session, "S2")
    result = await create_proposal(session, make_signal(strategy.id), mode="paper")
    assert result is None


async def test_signal_at_gate_s3_creates_proposal_in_z0_with_one_log_entry(session):
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await session.commit()

    assert proposal is not None
    assert proposal.state == STATE_ENTSTANDEN
    assert proposal.strategy_gate_status == "S3"

    events = await events_of(session, proposal.id)
    assert len(events) == 1
    assert events[0].from_state is None
    assert events[0].to_state == STATE_ENTSTANDEN


async def test_demotion_after_creation_does_not_change_existing_proposal(session):
    """Testfall 4: Proposal entsteht bei S3, Strategie wird danach auf S2 zurückgestuft."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await session.commit()

    strategy.gate_status = "S2"
    await session.commit()
    await session.refresh(proposal)

    assert proposal.strategy_gate_status == "S3"


async def test_new_signal_after_demotion_creates_no_proposal(session):
    """Testfall 5: nach Rückstufung auf S2 sieht ein neues Signal den aktuellen Status."""
    strategy = await make_strategy(session, "S3")
    await create_proposal(session, make_signal(strategy.id, "sig-1"), mode="paper")
    await session.commit()

    strategy.gate_status = "S2"
    await session.commit()

    result = await create_proposal(session, make_signal(strategy.id, "sig-2"), mode="paper")
    assert result is None


# --- Zustandsautomat (Testfälle 6-8) -----------------------------------------------------


async def test_disallowed_transition_is_rejected_and_state_unchanged(session):
    """Testfall 6: Z2 -> Z3 ist in Tabelle 4.2 nicht erlaubt."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await transition(
        session, proposal, STATE_STUFE1_ABGELEHNT, actor="system", reason="Limit gebrochen"
    )
    await session.commit()

    with pytest.raises(InvalidTransition):
        await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")

    assert proposal.state == STATE_STUFE1_ABGELEHNT
    # Der abgelehnte Versuch schreibt keinen Protokolleintrag.
    events = await events_of(session, proposal.id)
    assert [e.to_state for e in events] == [STATE_ENTSTANDEN, STATE_STUFE1_ABGELEHNT]


async def test_transition_out_of_a_terminal_state_is_rejected(session):
    """Testfall 7: Übergangsversuch aus einem Endzustand heraus, z. B. Z4 -> irgendetwas."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session, proposal, STATE_VERFALLEN, actor="system", reason="2h ohne Entscheidung"
    )
    await session.commit()

    with pytest.raises(InvalidTransition):
        await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")

    assert proposal.state == STATE_VERFALLEN


async def test_every_allowed_transition_writes_exactly_one_log_entry(session):
    """Testfall 8: jeder erlaubte Übergang -> genau ein unveränderlicher Protokolleintrag
    mit Vorzustand, Nachzustand, Zeitstempel, Auslöser, Grund."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await transition(
        session, proposal, STATE_STUFE1_LAEUFT, actor="system", reason="Stufe 1 gestartet"
    )
    await transition(
        session, proposal, STATE_WARTET_AUF_BOARD, actor="system", reason="Stufe 1 bestanden"
    )
    await session.commit()

    events = await events_of(session, proposal.id)
    assert len(events) == 3
    assert [(e.from_state, e.to_state) for e in events] == [
        (None, STATE_ENTSTANDEN),
        (STATE_ENTSTANDEN, STATE_STUFE1_LAEUFT),
        (STATE_STUFE1_LAEUFT, STATE_WARTET_AUF_BOARD),
    ]
    for event in events:
        assert event.occurred_at is not None
        assert event.actor == "system"
    assert events[1].reason == "Stufe 1 gestartet"
    assert events[2].reason == "Stufe 1 bestanden"


# --- Board-Änderung / Z6 (Testfall 9) ----------------------------------------------------


async def test_board_change_ends_parent_in_z6_and_branches_a_new_z0_record(session):
    strategy = await make_strategy(session, "S3")
    parent = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await transition(session, parent, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, parent, STATE_WARTET_AUF_BOARD, actor="system")
    await session.commit()

    child = await board_change(
        session,
        parent,
        actor="board:alice",
        reason="Größe angepasst",
        size=Decimal("0.5"),
    )
    await session.commit()

    assert parent.state == STATE_BOARD_GEAENDERT
    assert child.id != parent.id
    assert child.parent_proposal_id == parent.id
    assert child.change_generation == parent.change_generation + 1 == 1
    assert child.state == STATE_ENTSTANDEN
    assert child.size == Decimal("0.5")
    assert child.strategy_gate_status == parent.strategy_gate_status == "S3"

    child_events = await events_of(session, child.id)
    assert len(child_events) == 1
    assert child_events[0].from_state is None
    assert child_events[0].to_state == STATE_ENTSTANDEN


# --- Invariante 4.4.6: strategy_gate_status ist nach Anlage unveränderlich --------------


async def test_strategy_gate_status_cannot_be_mutated_after_creation(session):
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await session.commit()

    with pytest.raises(ValueError, match="unveränderlich"):
        proposal.strategy_gate_status = "S2"
