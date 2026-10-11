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
    STATE_BOARD_FREIGEGEBEN,
    STATE_BOARD_GEAENDERT,
    STATE_BOARD_VERWORFEN,
    STATE_ENTSTANDEN,
    STATE_STUFE1_ABGELEHNT,
    STATE_STUFE1_LAEUFT,
    STATE_STUFE2_ABGELEHNT,
    STATE_STUFE2_LAEUFT,
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


# --- HED-46: Vierwertiges Freigabe-Protokoll (Entwurf Abschnitt 8, Testfälle 1-5, 9) ---


async def test_stage1_rejection_logs_category_without_stage2_snapshot(session):
    """Testfall 1: Übergang nach Z2 erzeugt genau einen Eintrag mit

    log_category=stage1_rejection; limit_utilization_stage2 ist null, weil Stufe 2
    bei einer Stufe-1-Ablehnung nie läuft."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    proposal.limit_utilization = [{"limit": "summenrisiko", "auslastung_pct": 120}]
    await transition(
        session,
        proposal,
        STATE_STUFE1_ABGELEHNT,
        actor="system",
        reason="Limit gebrochen",
        rejection_reason_code="summenrisiko_ueberschritten",
    )
    await session.commit()

    events = await events_of(session, proposal.id)
    rejection_event = events[-1]
    assert rejection_event.log_category == "stage1_rejection"
    assert rejection_event.proposal_snapshot["limit_utilization_stage2"] is None
    assert rejection_event.proposal_snapshot["limit_utilization_stage1"] == [
        {"limit": "summenrisiko", "auslastung_pct": 120}
    ]
    assert proposal.log_category == "stage1_rejection"
    assert proposal.rejection_reason_code == "summenrisiko_ueberschritten"


async def test_stage2_rejection_logs_category_with_both_snapshots(session):
    """Testfall 2: Übergang nach Z9 erzeugt einen Eintrag mit

    log_category=stage2_rejection und beiden Limitauslastungs-Snapshots gesetzt."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    proposal.limit_utilization = [{"limit": "summenrisiko", "auslastung_pct": 80}]
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice")
    await transition(session, proposal, STATE_STUFE2_LAEUFT, actor="system")
    proposal.stage2_limit_utilization = [{"limit": "summenrisiko", "auslastung_pct": 105}]
    await transition(
        session,
        proposal,
        STATE_STUFE2_ABGELEHNT,
        actor="system",
        reason="Kurs zu alt",
        rejection_reason_code="kursfrische",
    )
    await session.commit()

    events = await events_of(session, proposal.id)
    rejection_event = events[-1]
    assert rejection_event.log_category == "stage2_rejection"
    assert rejection_event.proposal_snapshot["limit_utilization_stage1"] == [
        {"limit": "summenrisiko", "auslastung_pct": 80}
    ]
    assert rejection_event.proposal_snapshot["limit_utilization_stage2"] == [
        {"limit": "summenrisiko", "auslastung_pct": 105}
    ]


async def test_board_decision_targets_log_decided_by_system_frist_or_board(session):
    """Testfall 3: Übergänge nach Z4, Z5, Z6 erzeugen je einen Eintrag mit

    log_category=board_decision; decided_by ist system_frist bei Z4 (Fristablauf) und
    board bei Z5 (Verwerfen) / Z6 (Ändern)."""
    strategy = await make_strategy(session, "S3")

    expired = await create_proposal(session, make_signal(strategy.id, "sig-expire"), mode="paper")
    await transition(session, expired, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, expired, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session, expired, STATE_VERFALLEN, actor="system_frist", reason="2h ohne Entscheidung"
    )

    discarded = await create_proposal(
        session, make_signal(strategy.id, "sig-discard"), mode="paper"
    )
    await transition(session, discarded, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, discarded, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(
        session,
        discarded,
        STATE_BOARD_VERWORFEN,
        actor="board:alice",
        reason="Zu riskant",
        board_action="discard",
    )

    changed = await create_proposal(session, make_signal(strategy.id, "sig-change"), mode="paper")
    await transition(session, changed, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, changed, STATE_WARTET_AUF_BOARD, actor="system")
    await board_change(
        session, changed, actor="board:bob", reason="Größe angepasst", size=Decimal("1")
    )
    await session.commit()

    for proposal, expected_decided_by, expected_board_action in (
        (expired, "system_frist", None),
        (discarded, "board:alice", "discard"),
        (changed, "board:bob", "change"),
    ):
        assert proposal.log_category == "board_decision"
        assert proposal.decided_by == expected_decided_by
        assert proposal.decided_at is not None
        assert proposal.board_action == expected_board_action
        events = await events_of(session, proposal.id)
        assert events[-1].log_category == "board_decision"


async def test_no_transition_outside_the_five_target_states_sets_log_category(session):
    """Testfälle 4/5: nur Übergänge nach Z2/Z9/Z4/Z5/Z6 tragen eine der drei

    Kategorien; jeder dieser fünf Übergänge trägt genau eine, kein anderer Übergang
    (inkl. Z7 "freigegeben") trägt irgendeine."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
    await transition(session, proposal, STATE_BOARD_FREIGEGEBEN, actor="board:alice")
    await session.commit()

    events = await events_of(session, proposal.id)
    categorized_targets = {STATE_STUFE1_ABGELEHNT, STATE_STUFE2_ABGELEHNT, STATE_VERFALLEN,
                           STATE_BOARD_VERWORFEN, STATE_BOARD_GEAENDERT}
    for event in events:
        if event.to_state in categorized_targets:
            assert event.log_category is not None
        else:
            assert event.log_category is None
    # Z7 "freigegeben" ist explizit keine der drei Kategorien (Tabelle 8.2).
    assert events[-1].to_state == STATE_BOARD_FREIGEGEBEN
    assert events[-1].log_category is None


async def test_correction_creates_new_record_old_stays_unchanged(session):
    """Testfall 9: eine Korrektur eines bestehenden Eintrags erzeugt einen neuen

    Datensatz mit correction_of_id, der alte bleibt unverändert lesbar."""
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await transition(
        session, proposal, STATE_STUFE1_ABGELEHNT, actor="system", reason="Limit gebrochen"
    )
    await session.commit()

    original = (await events_of(session, proposal.id))[-1]
    original_reason = original.reason

    correction = ProposalEvent(
        proposal_id=proposal.id,
        from_state=original.from_state,
        to_state=original.to_state,
        actor="system",
        reason="Korrektur: falscher Grund-Code gespeichert",
        log_category=original.log_category,
        correction_of_id=original.id,
    )
    session.add(correction)
    await session.commit()

    await session.refresh(original)
    assert original.reason == original_reason
    assert original.correction_of_id is None

    events = await events_of(session, proposal.id)
    assert events[-1].correction_of_id == original.id


# --- Invariante 4.4.6: strategy_gate_status ist nach Anlage unveränderlich --------------


async def test_strategy_gate_status_cannot_be_mutated_after_creation(session):
    strategy = await make_strategy(session, "S3")
    proposal = await create_proposal(session, make_signal(strategy.id), mode="paper")
    await session.commit()

    with pytest.raises(ValueError, match="unveränderlich"):
        proposal.strategy_gate_status = "S2"
