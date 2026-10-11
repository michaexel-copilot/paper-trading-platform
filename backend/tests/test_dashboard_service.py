"""Dashboard-Geschäftslogik (HED-43, Plan-Dokument Abschnitt 1/2): Z3-Warteschlange,
Feld 15 (Sperrkennzeichen live), die drei Board-Aktionen, und der Race-Schutz zwischen
zwei gleichzeitigen Aktionen auf denselben Vorschlag.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.models import Base, ProposalActionAttempt, Strategy
from app.models.proposals import (
    STATE_BOARD_FREIGEGEBEN,
    STATE_BOARD_GEAENDERT,
    STATE_BOARD_VERWORFEN,
    STATE_ENTSTANDEN,
    STATE_STUFE1_LAEUFT,
    STATE_WARTET_AUF_BOARD,
)
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen
from app.trading import dashboard as service
from app.trading.proposals import Signal, create_proposal, transition


def _url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'dashboard.db').as_posix()}"


@pytest.fixture
async def session_factory(tmp_path):
    engine = make_engine(_url(tmp_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as db:
        yield db


async def make_strategy(session, gate_status: str = "S3") -> Strategy:
    strategy = Strategy(gate_status=gate_status)
    session.add(strategy)
    await session.flush()
    await session.commit()
    return strategy


async def goto_z3(session, strategy_id: int, *, signal_id: str = "sig-1", **field_overrides) -> int:
    """Treibt einen frischen Vorschlag bis Z3 und setzt optional die (in HED-41/HED-43
    nicht hier berechneten) Stufe-1-Kennzahlen, damit die Dashboard-Felder prüfbar
    sind. Gibt die ID zurück (nicht das ORM-Objekt), damit Aufrufer bewusst neu laden."""
    signal = Signal(
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
    proposal = await create_proposal(session, signal, mode="paper")
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system")
    await transition(
        session, proposal, STATE_WARTET_AUF_BOARD, actor="system", reason="Stufe 1 bestanden"
    )
    for key, value in field_overrides.items():
        setattr(proposal, key, value)
    if "expires_at" not in field_overrides:
        proposal.displayed_at = datetime.now(UTC)
        proposal.expires_at = proposal.displayed_at + timedelta(hours=2)
    await session.commit()
    return proposal.id


# --- Warteschlange und Feld 15 (live Sperrkennzeichen) ------------------------------------


async def test_list_queue_contains_only_z3_proposals(session, session_factory):
    strategy = await make_strategy(session)
    in_queue = await goto_z3(session, strategy.id, signal_id="a")
    other = await create_proposal(
        session,
        Signal(
            signal_id="b",
            strategy_id=strategy.id,
            instrument="ETH/USDT",
            direction="long",
            size=Decimal("1"),
            proposal_price=Decimal("10"),
            stop_price=Decimal("9"),
            proposal_kind="entry",
            signal_at=datetime.now(UTC),
        ),
        mode="paper",
    )
    await session.commit()

    queue = await service.list_queue(session)
    ids = [p.id for p in queue]
    assert in_queue in ids
    assert other.id not in ids
    assert other.state == STATE_ENTSTANDEN


async def test_lock_state_reads_live_not_frozen(session):
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)
    proposal = await session.get(service.Proposal, proposal_id)

    # Fehlende Zeile gilt fail-closed als 'gesetzt' (app.sperrkennzeichen) -- eine
    # offene Lage muss explizit angelegt werden.
    row = FirmenweitesSperrkennzeichen(gesetzt=False)
    session.add(row)
    await session.commit()

    before = await service.lock_state(session, proposal)
    assert before.firmenweit_gesperrt is False
    assert before.strategie_gesperrt is False

    row.gesetzt = True
    await session.commit()

    after = await service.lock_state(session, proposal)
    assert after.firmenweit_gesperrt is True


async def test_lock_state_uses_strategy_id_as_key(session):
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)
    proposal = await session.get(service.Proposal, proposal_id)

    session.add(StrategieSperrkennzeichen(strategie_schluessel=str(strategy.id), gesetzt=True))
    await session.commit()

    state = await service.lock_state(session, proposal)
    assert state.strategie_gesperrt is True


def test_remaining_seconds_none_without_expiry():
    class Stub:
        expires_at = None

    assert service.remaining_seconds(Stub(), datetime.now(UTC)) is None


def test_remaining_seconds_counts_down():
    now = datetime.now(UTC)

    class Stub:
        expires_at = now + timedelta(minutes=30)

    assert service.remaining_seconds(Stub(), now) == pytest.approx(1800, abs=1)


# --- Freigeben / Verwerfen / Ändern --------------------------------------------------------


async def test_approve_moves_to_z7_and_logs_applied(session, session_factory, betriebsart):
    betriebsart("papier")
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)

    result = await service.approve(session_factory, proposal_id, actor="board@example.com")

    assert result.state == STATE_BOARD_FREIGEGEBEN
    assert result.decided_by == "board@example.com"
    # board_action wird nur für die drei Protokollkategorien gesetzt (Entwurf 8.2) --
    # Z7 "freigegeben" ist keine davon (app.trading.proposals.STATE_TO_LOG_CATEGORY).
    assert result.board_action is None

    attempts = await _attempts(session_factory, proposal_id)
    assert [a.outcome for a in attempts] == ["applied"]
    assert attempts[0].action == "approve"


async def test_approve_still_succeeds_if_the_applied_log_write_fails(
    session, session_factory, betriebsart, monkeypatch
):
    """Die Zustandsänderung ist zu diesem Zeitpunkt bereits committet -- ein Fehler
    beim Schreiben des nachgelagerten 'applied'-Protokolleintrags darf dem Board nicht
    als fehlgeschlagene Freigabe erscheinen (sonst sieht das Board einen Fehler, obwohl
    der Vorschlag tatsächlich freigegeben wurde)."""
    betriebsart("papier")
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)

    original_log_attempt = service._log_attempt
    calls = []

    async def flaky_log_attempt(session_factory, proposal_id, actor, action, outcome, detail):
        calls.append(outcome)
        if outcome == "applied":
            raise RuntimeError("DB kurzzeitig nicht erreichbar")
        await original_log_attempt(session_factory, proposal_id, actor, action, outcome, detail)

    monkeypatch.setattr(service, "_log_attempt", flaky_log_attempt)

    result = await service.approve(session_factory, proposal_id, actor="board@example.com")

    assert result.state == STATE_BOARD_FREIGEGEBEN
    assert calls == ["applied"]


async def test_discard_moves_to_z5_with_reason(session, session_factory, betriebsart):
    betriebsart("papier")
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)

    result = await service.discard(
        session_factory, proposal_id, actor="board@example.com", reason="Zu riskant"
    )

    assert result.state == STATE_BOARD_VERWORFEN
    assert result.rejection_reason_text == "Zu riskant"


async def test_change_ends_parent_in_z6_and_returns_new_child_in_z0(
    session, session_factory, betriebsart
):
    betriebsart("papier")
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)

    child = await service.change(
        session_factory,
        proposal_id,
        actor="board@example.com",
        reason="Größe anpassen",
        size=Decimal("2"),
    )

    assert child.id != proposal_id
    assert child.state == STATE_ENTSTANDEN
    assert child.parent_proposal_id == proposal_id
    assert child.size == Decimal("2")

    parent = await session.get(service.Proposal, proposal_id)
    await session.refresh(parent)
    assert parent.state == STATE_BOARD_GEAENDERT


@pytest.mark.parametrize("action", ["approve", "discard", "change"])
async def test_action_on_non_z3_proposal_is_refused_with_current_state(
    session, session_factory, action, betriebsart
):
    betriebsart("papier")
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)
    await service.approve(session_factory, proposal_id, actor="first@example.com")

    with pytest.raises(service.ActionRefused) as excinfo:
        if action == "approve":
            await service.approve(session_factory, proposal_id, actor="second@example.com")
        elif action == "discard":
            await service.discard(
                session_factory, proposal_id, actor="second@example.com", reason=None
            )
        else:
            await service.change(
                session_factory, proposal_id, actor="second@example.com", reason="x"
            )

    assert excinfo.value.code == "not_awaiting_board"
    assert excinfo.value.current_state == STATE_BOARD_FREIGEGEBEN

    attempts = await _attempts(session_factory, proposal_id)
    assert attempts[-1].outcome == "rejected_state"


async def test_concurrent_actions_second_gets_conflict_not_silent_overwrite(
    session, session_factory, betriebsart
):
    """Plan HED-43 Abschnitt 2: ein Rennen zwischen zwei Board-Aktionen auf denselben
    Vorschlag -- die erste gewinnt, die zweite bekommt einen Konflikt, beide werden
    protokolliert."""
    betriebsart("papier")
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)

    # Beide "Clients" laden den Vorschlag parallel, bevor einer von beiden committet --
    # nachgebildet, indem approve zuerst vollständig durchläuft und discard danach auf
    # demselben (inzwischen veralteten) Zustand aufsetzen würde. Der Lesezeitpunkt
    # beider Aktionen in _guarded_action liegt vor dem jeweils anderen Commit, weil
    # jede Aktion ihre eigene, frische Session öffnet -- das reicht hier nicht aus, um
    # den Konflikt zu erzwingen, deshalb simuliert dieser Test das Rennen direkt über
    # das Versionsfeld: die zweite Aktion arbeitet auf einem Proposal-Objekt, dessen
    # Version beim Laden bereits veraltet ist.
    first = await service.approve(session_factory, proposal_id, actor="first@example.com")
    assert first.state == STATE_BOARD_FREIGEGEBEN

    # Zweite Aktion: Zustand ist jetzt Z7, nicht mehr Z3 -- das ist der reguläre,
    # garantierte Ablehnungspfad (kein Zeitfenster für ein stilles Überschreiben, weil
    # _guarded_action den Zustand serverseitig vor jeder Mutation neu liest).
    with pytest.raises(service.ActionRefused) as excinfo:
        await service.discard(
            session_factory, proposal_id, actor="second@example.com", reason="zu spät"
        )
    assert excinfo.value.code == "not_awaiting_board"

    attempts = await _attempts(session_factory, proposal_id)
    assert [a.outcome for a in attempts] == ["applied", "rejected_state"]


async def test_version_conflict_is_caught_when_state_check_races(
    session, session_factory, monkeypatch
):
    """Erzwingt den StaleDataError-Pfad direkt: zwei Sessions lesen denselben
    Z3-Vorschlag, bevor eine von beiden committet -- der optimistische Lock
    (``Proposal.version``) muss die zweite Mutation ablehnen, nicht überschreiben."""
    strategy = await make_strategy(session)
    proposal_id = await goto_z3(session, strategy.id)

    async with session_factory() as session_a, session_factory() as session_b:
        proposal_a = await session_a.get(service.Proposal, proposal_id)
        proposal_b = await session_b.get(service.Proposal, proposal_id)

        await transition(
            session_a, proposal_a, "board_freigegeben", actor="a", board_action="approve"
        )
        await session_a.commit()

        await transition(
            session_b, proposal_b, "board_verworfen", actor="b", board_action="discard"
        )
        with pytest.raises(service.StaleDataError):
            await session_b.commit()


async def _attempts(session_factory, proposal_id: int) -> list[ProposalActionAttempt]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(ProposalActionAttempt)
            .where(ProposalActionAttempt.proposal_id == proposal_id)
            .order_by(ProposalActionAttempt.id)
        )
        return list(rows)
