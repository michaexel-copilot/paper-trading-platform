"""Protokoll für Kategorie (iv) des Freigabe-Protokolls: automatischer

strategiegetriebener Ausstieg (HED-46 / HED-36 Entwurf Fassung 1.1 Abschnitt 8.4).
Deckt die Testfälle 6 und 7 aus dem Dokument "spezifikation" auf HED-46 ab. Die
Prüfkette, die eine solche Zeile tatsächlich auslöst, ist HED-48 und nicht Teil
dieses Tests -- hier wird nur das Protokollformat selbst geprüft.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect

from app.db import make_engine, make_session_factory
from app.models import AutoExitLogEntry, Base, ProposalEvent, Strategy


def _url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'auto_exit.db').as_posix()}"


@pytest.fixture
async def session(tmp_path):
    engine = make_engine(_url(tmp_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as db:
        yield db
    await engine.dispose()


async def test_full_auto_exit_entry_has_all_required_fields_set(session):
    """Testfall 6: ein vollständiger AutoExitLogEntry mit result=gefuellt enthält alle

    Pflichtfelder aus Abschnitt 4 befüllt, keines null außer den laut Tabelle nullable
    markierten."""
    strategy = Strategy(gate_status="S3")
    session.add(strategy)
    await session.flush()

    now = datetime.now(UTC)
    entry = AutoExitLogEntry(
        mode="paper",
        strategy_id=strategy.id,
        trigger_signal_id="sig-exit-1",
        trigger_signal_kind="stop_erreicht",
        trigger_at=now,
        sent_at=now,
        filled_at=now,
        instrument="BTC/USDT",
        exit_size=Decimal("0.5"),
        position_before=Decimal("0.5"),
        position_after=Decimal("0"),
        order_direction="verkauf",
        position_direction_closed="long",
        price_age_seconds=3,
        price_source_timestamp=now,
        size_was_capped=False,
        is_reversal=False,
        firmwide_lock_state="offen",
        strategy_lock_state="offen",
        result="gefuellt",
    )
    session.add(entry)
    await session.commit()

    required_fields = [
        "mode",
        "strategy_id",
        "trigger_signal_id",
        "trigger_signal_kind",
        "trigger_at",
        "instrument",
        "exit_size",
        "position_before",
        "order_direction",
        "position_direction_closed",
        "price_age_seconds",
        "price_source_timestamp",
        "size_was_capped",
        "is_reversal",
        "firmwide_lock_state",
        "strategy_lock_state",
        "result",
        "created_at",
    ]
    for field in required_fields:
        assert getattr(entry, field) is not None, f"{field} darf nicht null sein"

    # Nullable laut Tabelle (Entwurf 8.4), hier bewusst nicht gesetzt oder gleich null:
    assert entry.size_capped_to is None
    assert entry.reversal_counter_proposal_id is None
    assert entry.escalation_at is None
    assert entry.escalation_recipients is None
    assert entry.escalation_deadline is None
    assert entry.escalation_result is None
    assert entry.correction_of_id is None


async def test_proposal_events_and_auto_exit_entries_cannot_be_summed_into_one_status(
    session,
):
    """Testfall 7: ProposalEvent und AutoExitLogEntry sind in keiner Abfrage zu einer

    gemeinsamen "abgelehnt"-Summe zusammenfassbar -- Schema-Test: keine Sicht/kein
    Report-Feld vereint beide (Entwurf 8.1: die vier Kategorien bleiben immer getrennt)."""
    proposal_event_columns = {c.key for c in inspect(ProposalEvent).columns}
    auto_exit_columns = {c.key for c in inspect(AutoExitLogEntry).columns}

    # Die beiden Entities teilen keine Tabelle, keinen gemeinsamen Status-Spaltennamen
    # mit identischer Bedeutung (log_category existiert nur bei ProposalEvent; result
    # ist ein eigenes Vokabular bei AutoExitLogEntry) und keine ORM-Relationship, über
    # die eine Abfrage beide in einer Zeile zusammenführen könnte.
    assert ProposalEvent.__tablename__ != AutoExitLogEntry.__tablename__
    assert "log_category" in proposal_event_columns
    assert "log_category" not in auto_exit_columns
    assert "result" in auto_exit_columns
    assert "result" not in proposal_event_columns
    assert not hasattr(ProposalEvent, "auto_exit_entries")
    assert not hasattr(AutoExitLogEntry, "proposal_event")
