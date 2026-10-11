"""Board-Benachrichtigung (HED-43, Entwurf 5.3): Versand, genau eine Wiederholung bei
Fehler, dauerhafter Ausfall löst Einzelmeldung an den CRO aus. Kein echter SMTP-Server
-- ``app.notify._send_blocking`` wird durch einen Fake ersetzt.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import notify
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import Base, BoardNotification, Strategy
from app.models.proposals import STATE_STUFE1_LAEUFT, STATE_WARTET_AUF_BOARD
from app.trading.proposals import Signal, create_proposal, transition


def _url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'notify.db').as_posix()}"


@pytest.fixture
async def session_factory(tmp_path):
    engine = make_engine(_url(tmp_path))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        board_notify_emails="board@example.com,cfo@example.com",
        cro_alert_email="cro@example.com",
        dashboard_base_url="https://dashboard.example.com",
    )


@pytest.fixture
async def proposal_id(session_factory, betriebsart):
    betriebsart("papier")
    async with session_factory() as session:
        strategy = Strategy(gate_status="S3")
        session.add(strategy)
        await session.flush()
        signal = Signal(
            signal_id="sig-1",
            strategy_id=strategy.id,
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
        await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system")
        await session.commit()
        return proposal.id


async def _notification(session_factory, proposal_id: int) -> BoardNotification:
    async with session_factory() as session:
        return await session.scalar(
            select(BoardNotification).where(BoardNotification.proposal_id == proposal_id)
        )


async def test_successful_send_marks_sent_after_one_attempt(
    session_factory, settings, proposal_id, monkeypatch
):
    calls = []
    monkeypatch.setattr(notify, "_send_blocking", lambda s, msg: calls.append(msg))

    await notify.notify_new_proposal(session_factory, settings, proposal_id)

    record = await _notification(session_factory, proposal_id)
    assert record.status == "sent"
    assert record.attempts == 1
    assert record.sent_at is not None
    assert len(calls) == 1
    assert "board@example.com" in calls[0]["To"]


async def test_failure_then_success_retries_once(
    session_factory, settings, proposal_id, monkeypatch
):
    attempts = {"n": 0}

    def flaky(s, msg):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("connection refused")

    monkeypatch.setattr(notify, "_send_blocking", flaky)

    await notify.notify_new_proposal(session_factory, settings, proposal_id)

    record = await _notification(session_factory, proposal_id)
    assert record.status == "sent"
    assert record.attempts == 2


async def test_persistent_failure_marks_failed_and_alerts_cro(
    session_factory, settings, proposal_id, monkeypatch
):
    def always_fails(s, msg):
        raise OSError("connection refused")

    cro_alerts = []

    def capture_cro(s, msg):
        if msg["To"] == settings.cro_alert_email:
            cro_alerts.append(msg)
            return
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "_send_blocking", capture_cro)

    await notify.notify_new_proposal(session_factory, settings, proposal_id)

    record = await _notification(session_factory, proposal_id)
    assert record.status == "failed"
    assert record.attempts == 2
    assert record.last_error is not None
    assert record.cro_alert_sent is True
    assert len(cro_alerts) == 1

    failing = await notify.pending_notification_count(session_factory)
    assert failing == 1


async def test_second_concurrent_call_does_not_double_send(
    session_factory, settings, proposal_id, monkeypatch
):
    """Zwei sich überlappende Poller-Durchläufe dürfen nicht beide senden -- die
    BoardNotification-Zeile mit unique(proposal_id) wird zuerst angelegt, der zweite
    Versuch committet nicht und sendet nicht."""
    calls = []
    monkeypatch.setattr(notify, "_send_blocking", lambda s, msg: calls.append(msg))

    await asyncio.gather(
        notify.notify_new_proposal(session_factory, settings, proposal_id),
        notify.notify_new_proposal(session_factory, settings, proposal_id),
    )

    assert len(calls) == 1
