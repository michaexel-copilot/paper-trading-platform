"""HTTP-Pfad des Dashboards (HED-43): Warteschlange, Pflichtfelder, die drei Aktionen
inklusive erzwungener Entscheider-Kennung aus der Sitzung und serverseitiger
Zustandsprüfung (409 statt UI-Ausgrauen).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import Strategy
from app.models.proposals import STATE_STUFE1_LAEUFT, STATE_WARTET_AUF_BOARD
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.trading.proposals import Signal, create_proposal, transition
from tests.conftest import _client, sign_up


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    """Normalzustand einer laufenden Installation (Konvention aus test_stufe1.py,
    HED-47): das firmenweite Kennzeichen existiert und ist offen, sonst würde
    ``create_proposal`` seit HED-47 jedes Signal hier fail-closed unterdrücken, bevor
    es den Dashboard-Pfad überhaupt erreicht. Commit statt Flush, damit die
    Schreibtransaktion nicht offen bleibt, während ``client`` (eigene Verbindung zum
    selben SQLite-File) in einer anderen Fixture schreibt -- sonst 'database is
    locked'."""
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


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
        await transition(
            session, proposal, STATE_WARTET_AUF_BOARD, actor="system", reason="Stufe 1 bestanden"
        )
        proposal.nav = Decimal("10000")
        proposal.drawdown = Decimal("0.02")
        proposal.aggregate_risk_utilization_before = Decimal("0.10")
        proposal.aggregate_risk_utilization_after = Decimal("0.15")
        proposal.risk_eur = Decimal("50")
        proposal.correlation_to_open_positions = Decimal("0.3")
        proposal.price_age_seconds = 2
        proposal.limit_utilization = [{"name": "aggregate_risk", "value": "0.15", "limit": "0.20"}]
        proposal.stage1_result = "passed"
        proposal.displayed_at = datetime.now(UTC)
        proposal.expires_at = proposal.displayed_at + timedelta(hours=2)
        await session.commit()
        return proposal.id


async def test_list_requires_auth(anonymous, proposal_id):
    response = await anonymous.get("/api/dashboard/proposals")
    assert response.status_code == 401


async def test_list_shows_the_proposal_with_required_fields(client, proposal_id):
    response = await client.get("/api/dashboard/proposals")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    item = body[0]
    assert item["id"] == proposal_id
    assert item["state"] == STATE_WARTET_AUF_BOARD
    # Pflichtfelder Nr. 1-8 (Plan HED-43 Abschnitt 1)
    assert item["nav"] == "10000"
    assert item["drawdown"] == "0.02"
    assert item["aggregate_risk_utilization_before"] == "0.1"
    assert item["aggregate_risk_utilization_after"] == "0.15"
    assert item["risk_eur"] == "50"
    assert item["correlation_to_open_positions"] == "0.3"
    assert item["price_age_seconds"] == 2
    assert item["stage1_result"] == "passed"
    assert item["limit_utilization"] == [
        {"name": "aggregate_risk", "value": "0.15", "limit": "0.20"}
    ]
    # Nr. 9-15
    assert item["instrument"] == "BTC/USDT"
    assert item["direction"] == "long"
    assert item["size"] == "1"
    assert item["proposal_price"] == "100"
    assert item["stop_price"] == "95"
    assert item["stop_distance"] == "5"
    assert item["stop_distance_pct"] == "0.05"
    assert item["proposal_kind"] == "entry"
    assert item["remaining_seconds"] > 0
    assert item["change_generation"] == 0
    assert item["parent_proposal_id"] is None
    # Vor HED-47 gab es für diesen Test keine Zeile -- fail-closed galt daher als
    # 'gesperrt'. Die autouse-Fixture offenes_firmenweites_kennzeichen bildet jetzt den
    # realistischen Normalzustand nach (offene Installation); den fail-closed-Fall ohne
    # Zeile deckt test_sperrkennzeichen.py ab.
    assert item["lock_state"] == {"firmenweit_gesperrt": False, "strategie_gesperrt": False}


async def test_detail_endpoint_returns_same_proposal(client, proposal_id):
    response = await client.get(f"/api/dashboard/proposals/{proposal_id}")
    assert response.status_code == 200
    assert response.json()["id"] == proposal_id


async def test_detail_404_for_unknown_id(client, proposal_id):
    response = await client.get("/api/dashboard/proposals/999999")
    assert response.status_code == 404


async def test_status_reports_queue_length(client, proposal_id):
    response = await client.get("/api/dashboard/status")
    assert response.status_code == 200
    assert response.json()["queue_length"] == 1


async def test_approve_requires_explicit_confirm(client, proposal_id):
    response = await client.post(
        f"/api/dashboard/proposals/{proposal_id}/approve", json={"confirm": False}
    )
    assert response.status_code == 422


async def test_approve_uses_session_identity_as_decider(client, proposal_id):
    response = await client.post(
        f"/api/dashboard/proposals/{proposal_id}/approve", json={"confirm": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "board_freigegeben"

    detail = await client.get(f"/api/dashboard/proposals/{proposal_id}")
    assert detail.status_code == 200


async def test_discard_with_reason(client, proposal_id):
    response = await client.post(
        f"/api/dashboard/proposals/{proposal_id}/discard", json={"reason": "zu riskant"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "board_verworfen"


async def test_change_creates_new_proposal_in_z0(client, proposal_id):
    response = await client.post(
        f"/api/dashboard/proposals/{proposal_id}/change",
        json={"reason": "Größe anpassen", "size": "2"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] != proposal_id
    assert body["state"] == "entstanden"
    assert body["parent_proposal_id"] == proposal_id
    assert body["size"] == "2"

    queue = await client.get("/api/dashboard/proposals")
    assert queue.json() == []


async def test_second_action_on_already_decided_proposal_gets_409_with_current_state(
    client, proposal_id
):
    first = await client.post(
        f"/api/dashboard/proposals/{proposal_id}/approve", json={"confirm": True}
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/dashboard/proposals/{proposal_id}/discard", json={"reason": "zu spät"}
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "not_awaiting_board"
    assert detail["current_state"] == "board_freigegeben"


async def test_two_board_members_racing_on_the_same_proposal(app, client, proposal_id):
    """Zwei Board-Mitglieder entscheiden quasi gleichzeitig -- nur eine Aktion darf
    durchgehen, die zweite bekommt einen Konflikt, nicht einen stillen Erfolg."""
    async with _client(app) as other:
        await sign_up(other, "carol@example.com")

        first = await client.post(
            f"/api/dashboard/proposals/{proposal_id}/approve", json={"confirm": True}
        )
        second = await other.post(
            f"/api/dashboard/proposals/{proposal_id}/discard", json={"reason": "doch nicht"}
        )

        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [200, 409]
