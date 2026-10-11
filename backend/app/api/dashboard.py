"""Dashboard-Endpunkte: Freigeben/Ändern/Verwerfen (HED-43, Entwurf HED-36 Abschnitt 5).

Jede der drei Aktionen erzwingt serverseitig, dass der Vorschlag in Z3
"wartet_auf_board" ist (nicht nur im UI ausgegraut), und erzwingt serverseitig eine
Entscheider-Kennung -- hier die Identität aus der Sitzung (``current_user``), nicht ein
vom Client mitgeschicktes Freitextfeld. Ein Client kann sich damit nicht als jemand
anderes ausgeben.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import error, get_session_factory
from app.api.schemas import (
    ApproveIn,
    ChangeIn,
    DashboardStatusOut,
    DiscardIn,
    LockStateOut,
    ProposalOut,
)
from app.auth import current_user
from app.db import SessionFactory, get_session
from app.dbtypes import utcnow
from app.models import Proposal, User
from app.notify import pending_notification_count
from app.trading import dashboard as service

router = APIRouter(
    prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(current_user)]
)


async def _proposal_out(session: AsyncSession, proposal: Proposal) -> ProposalOut:
    now = utcnow()
    lock = await service.lock_state(session, proposal)
    return ProposalOut(
        id=proposal.id,
        state=proposal.state,
        mode=proposal.mode,
        strategy_id=proposal.strategy_id,
        signal_id=proposal.signal_id,
        parent_proposal_id=proposal.parent_proposal_id,
        change_generation=proposal.change_generation,
        instrument=proposal.instrument,
        direction=proposal.direction,
        size=proposal.size,
        proposal_price=proposal.proposal_price,
        stop_price=proposal.stop_price,
        stop_distance=proposal.stop_distance,
        stop_distance_pct=proposal.stop_distance_pct,
        proposal_kind=proposal.proposal_kind,
        limit_price=proposal.limit_price,
        nav=proposal.nav,
        drawdown=proposal.drawdown,
        aggregate_risk_utilization_before=proposal.aggregate_risk_utilization_before,
        aggregate_risk_utilization_after=proposal.aggregate_risk_utilization_after,
        risk_eur=proposal.risk_eur,
        correlation_to_open_positions=proposal.correlation_to_open_positions,
        price_age_seconds=proposal.price_age_seconds,
        limit_utilization=proposal.limit_utilization,
        stage1_result=proposal.stage1_result,
        signal_at=proposal.signal_at,
        displayed_at=proposal.displayed_at,
        expires_at=proposal.expires_at,
        remaining_seconds=service.remaining_seconds(proposal, now),
        lock_state=LockStateOut(
            firmenweit_gesperrt=lock.firmenweit_gesperrt, strategie_gesperrt=lock.strategie_gesperrt
        ),
    )


def _refused(exc: service.ActionRefused):
    return error(status.HTTP_409_CONFLICT, exc.code, str(exc), current_state=exc.current_state)


@router.get("/proposals", response_model=list[ProposalOut])
async def list_proposals(session: AsyncSession = Depends(get_session)):
    proposals = await service.list_queue(session)
    return [await _proposal_out(session, p) for p in proposals]


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
async def get_proposal(proposal_id: int, session: AsyncSession = Depends(get_session)):
    proposal = await service.get_proposal(session, proposal_id)
    if proposal is None:
        raise error(status.HTTP_404_NOT_FOUND, "not_found", "Proposal not found.")
    return await _proposal_out(session, proposal)


@router.get("/status", response_model=DashboardStatusOut)
async def dashboard_status(
    session: AsyncSession = Depends(get_session),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    queue = await service.list_queue(session)
    failing = await pending_notification_count(session_factory)
    return DashboardStatusOut(queue_length=len(queue), notifications_failing=failing)


@router.post("/proposals/{proposal_id}/approve", response_model=ProposalOut)
async def approve_proposal(
    proposal_id: int,
    body: ApproveIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    if not body.confirm:
        raise error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "confirmation_required", "confirm must be true."
        )
    try:
        proposal = await service.approve(session_factory, proposal_id, actor=user.email)
    except service.ActionRefused as exc:
        raise _refused(exc) from None
    return await _proposal_out(session, proposal)


@router.post("/proposals/{proposal_id}/discard", response_model=ProposalOut)
async def discard_proposal(
    proposal_id: int,
    body: DiscardIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    try:
        proposal = await service.discard(
            session_factory, proposal_id, actor=user.email, reason=body.reason
        )
    except service.ActionRefused as exc:
        raise _refused(exc) from None
    return await _proposal_out(session, proposal)


@router.post("/proposals/{proposal_id}/change", response_model=ProposalOut)
async def change_proposal(
    proposal_id: int,
    body: ChangeIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    try:
        proposal = await service.change(
            session_factory,
            proposal_id,
            actor=user.email,
            reason=body.reason,
            size=body.size,
            proposal_price=body.proposal_price,
            stop_price=body.stop_price,
        )
    except service.ActionRefused as exc:
        raise _refused(exc) from None
    return await _proposal_out(session, proposal)
