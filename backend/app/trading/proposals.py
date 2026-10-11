"""Vorschlag-Zustandsautomat und Gate-Weiche.

Umsetzung von HED-36 Entwurf Fassung 1.1, Abschnitt 4 (CRO-freigegeben), siehe HED-41.

Bewusst außerhalb dieses Umfangs (andere HED-36-Kinder):
- Doppelprüfung Sperrkennzeichen (firmenweit + Strategie) -- HED-47.
- Stufe-1-/Stufe-2-B-2-Prüfung selbst (Limit-Berechnung, Kursfrische, Preisband) -- HED-44.
- Versand an die Gegenstelle -- HED-42.
- Dashboard-Darstellung -- HED-36 Abschnitt 5.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.models.proposals import (
    ALLOWED_TRANSITIONS,
    STATE_ENTSTANDEN,
    STATE_TO_LOG_CATEGORY,
    STATE_WARTET_AUF_BOARD,
    Proposal,
    ProposalEvent,
)
from app.models.strategies import Strategy

PROPOSAL_VALIDITY = timedelta(hours=2)


class InvalidTransition(ValueError):
    """Ein Übergang, der nicht in ALLOWED_TRANSITIONS steht (Tabelle 4.2)."""


@dataclass(frozen=True)
class Signal:
    """Eingabe der Gate-Weiche. Entstehung/Inhalt eines Signals liegt außerhalb dieses
    Entwurfs (Strategieebene, nicht Ausführungsschicht) -- hier nur die Felder, die ein
    Vorschlag als Snapshot braucht (Entwurf 4.1)."""

    signal_id: str
    strategy_id: int
    instrument: str
    direction: str  # long | short
    size: Decimal
    proposal_price: Decimal
    stop_price: Decimal
    proposal_kind: str  # entry | increase
    signal_at: datetime


async def create_proposal(session: AsyncSession, signal: Signal, *, mode: str) -> Proposal | None:
    """Gate-Weiche vor Z0 (Entwurf 4.2a).

    Liest Strategy.gate_status genau einmal, jetzt. S1/S2-Signale erzeugen keinen
    Proposal-Datensatz und laufen unverändert über den bestehenden Paper-Forward-Test-Pfad.
    Nur S3 (oder höher) erzeugt einen Proposal in Z0, mit eingefrorenem
    strategy_gate_status (Festlegung 4.4.6).
    """
    gate_status = await session.scalar(
        select(Strategy.gate_status).where(Strategy.id == signal.strategy_id)
    )
    if gate_status in ("S1", "S2"):
        return None

    stop_distance = abs(signal.proposal_price - signal.stop_price)
    proposal = Proposal(
        mode=mode,
        strategy_id=signal.strategy_id,
        strategy_gate_status=gate_status,
        signal_id=signal.signal_id,
        change_generation=0,
        instrument=signal.instrument,
        direction=signal.direction,
        size=signal.size,
        proposal_price=signal.proposal_price,
        stop_price=signal.stop_price,
        stop_distance=stop_distance,
        stop_distance_pct=stop_distance / signal.proposal_price,
        proposal_kind=signal.proposal_kind,
        signal_at=signal.signal_at,
        state=STATE_ENTSTANDEN,
    )
    session.add(proposal)
    await session.flush()

    session.add(
        ProposalEvent(
            proposal_id=proposal.id,
            from_state=None,
            to_state=STATE_ENTSTANDEN,
            actor="system",
            reason="Signal angenommen, Strategiegate S3 oder höher",
        )
    )
    return proposal


def _proposal_snapshot(proposal: Proposal) -> dict:
    """Vollständige Kopie der zum Zeitpunkt des Protokolleintrags eingefrorenen

    Proposal-Felder (Entwurf 8.3): Handelsinhalt, Stufe-1- und, falls vorhanden,
    Stufe-2-Kennzahlen, Dashboard-Felder, relevante Zeitstempel. Werte, keine
    Referenzen -- siehe Kommentar auf ProposalEvent.proposal_snapshot.
    """
    return {
        "mode": proposal.mode,
        "strategy_id": proposal.strategy_id,
        "strategy_gate_status": proposal.strategy_gate_status,
        "signal_id": proposal.signal_id,
        "instrument": proposal.instrument,
        "direction": proposal.direction,
        "size": str(proposal.size),
        "proposal_price": str(proposal.proposal_price),
        "stop_price": str(proposal.stop_price),
        "stop_distance": str(proposal.stop_distance),
        "stop_distance_pct": str(proposal.stop_distance_pct),
        "proposal_kind": proposal.proposal_kind,
        "nav": str(proposal.nav) if proposal.nav is not None else None,
        "drawdown": str(proposal.drawdown) if proposal.drawdown is not None else None,
        "aggregate_risk_utilization_before": (
            str(proposal.aggregate_risk_utilization_before)
            if proposal.aggregate_risk_utilization_before is not None
            else None
        ),
        "aggregate_risk_utilization_after": (
            str(proposal.aggregate_risk_utilization_after)
            if proposal.aggregate_risk_utilization_after is not None
            else None
        ),
        "risk_eur": str(proposal.risk_eur) if proposal.risk_eur is not None else None,
        "correlation_to_open_positions": (
            str(proposal.correlation_to_open_positions)
            if proposal.correlation_to_open_positions is not None
            else None
        ),
        "price_age_seconds": proposal.price_age_seconds,
        "limit_utilization_stage1": proposal.limit_utilization,
        "limit_utilization_stage2": proposal.stage2_limit_utilization,
        "signal_at": proposal.signal_at.isoformat(),
        "stage1_at": proposal.stage1_at.isoformat() if proposal.stage1_at else None,
        "displayed_at": proposal.displayed_at.isoformat() if proposal.displayed_at else None,
        "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
        "stage2_at": proposal.stage2_at.isoformat() if proposal.stage2_at else None,
        "sent_at": proposal.sent_at.isoformat() if proposal.sent_at else None,
    }


async def transition(
    session: AsyncSession,
    proposal: Proposal,
    new_state: str,
    *,
    actor: str,
    reason: str | None = None,
    rejection_reason_code: str | None = None,
    board_action: str | None = None,
) -> Proposal:
    """Erzwingt ALLOWED_TRANSITIONS (Tabelle 4.2) und schreibt genau einen
    unveränderlichen Protokolleintrag je erlaubtem Übergang (Festlegung 4.4.8).

    Ein nicht erlaubter Übergang wird abgelehnt: kein Protokolleintrag, der Vorschlag
    bleibt in seinem bisherigen Zustand.

    `log_category` wird nie vom Aufrufer übergeben, sondern aus dem Zielzustand
    abgeleitet (STATE_TO_LOG_CATEGORY, Entwurf 8.2) -- so kann kein Übergang versehentlich
    in eine falsche oder in gar keine Kategorie fallen (Testfälle 4/5, HED-46 Abschnitt 8).
    """
    allowed = ALLOWED_TRANSITIONS.get(proposal.state, frozenset())
    if new_state not in allowed:
        raise InvalidTransition(
            f"Übergang {proposal.state} -> {new_state} ist nicht erlaubt (Tabelle 4.2)"
        )

    from_state = proposal.state
    proposal.state = new_state

    # Z3 "wartet_auf_board" ist der einzige Entscheidungspunkt des Boards (Freigeben,
    # Ändern, Verwerfen) bzw. des Fristablaufs -- unabhängig davon, ob der Zielzustand
    # eine der drei Protokollkategorien auslöst (Z7 "freigegeben" tut das nicht).
    if from_state == STATE_WARTET_AUF_BOARD:
        proposal.decided_at = utcnow()
        proposal.decided_by = actor

    log_category = STATE_TO_LOG_CATEGORY.get(new_state)
    proposal_snapshot = None
    if log_category is not None:
        proposal.log_category = log_category
        proposal.rejection_reason_code = rejection_reason_code
        proposal.rejection_reason_text = reason
        if board_action is not None:
            proposal.board_action = board_action
        proposal_snapshot = _proposal_snapshot(proposal)

    session.add(
        ProposalEvent(
            proposal_id=proposal.id,
            from_state=from_state,
            to_state=new_state,
            actor=actor,
            reason=reason,
            log_category=log_category,
            proposal_snapshot=proposal_snapshot,
        )
    )
    return proposal


async def board_change(
    session: AsyncSession, parent: Proposal, *, actor: str, reason: str, **overrides
) -> Proposal:
    """Board-Änderung (Z6, Testfall 9): der bestehende Datensatz endet in Z6
    `board_geaendert`; die "Fortsetzung" ist ein neuer Datensatz in Z0 mit
    `parent_proposal_id` und `change_generation + 1`, nicht ein Zustandswechsel des
    alten Datensatzes.

    `overrides` darf nur Handelsinhalt-Felder setzen (z. B. size, proposal_price,
    stop_price); strategy_gate_status wird vom Elterndatensatz übernommen, nicht neu
    aus Strategy gelesen, weil es keine neue Signalentstehung ist.
    """
    await transition(
        session,
        parent,
        "board_geaendert",
        actor=actor,
        reason=reason,
        board_action="change",
    )

    proposal_price = overrides.get("proposal_price", parent.proposal_price)
    stop_price = overrides.get("stop_price", parent.stop_price)
    size = overrides.get("size", parent.size)
    stop_distance = abs(proposal_price - stop_price)

    child = Proposal(
        mode=parent.mode,
        strategy_id=parent.strategy_id,
        strategy_gate_status=parent.strategy_gate_status,
        signal_id=parent.signal_id,
        parent_proposal_id=parent.id,
        change_generation=parent.change_generation + 1,
        instrument=parent.instrument,
        direction=parent.direction,
        size=size,
        proposal_price=proposal_price,
        stop_price=stop_price,
        stop_distance=stop_distance,
        stop_distance_pct=stop_distance / proposal_price,
        proposal_kind=parent.proposal_kind,
        signal_at=utcnow(),
        state=STATE_ENTSTANDEN,
    )
    session.add(child)
    await session.flush()

    session.add(
        ProposalEvent(
            proposal_id=child.id,
            from_state=None,
            to_state=STATE_ENTSTANDEN,
            actor=actor,
            reason=f"Board-Änderung von Vorschlag {parent.id}",
        )
    )
    return child
