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


async def transition(
    session: AsyncSession,
    proposal: Proposal,
    new_state: str,
    *,
    actor: str,
    reason: str | None = None,
    log_category: str | None = None,
) -> Proposal:
    """Erzwingt ALLOWED_TRANSITIONS (Tabelle 4.2) und schreibt genau einen
    unveränderlichen Protokolleintrag je erlaubtem Übergang (Festlegung 4.4.8).

    Ein nicht erlaubter Übergang wird abgelehnt: kein Protokolleintrag, der Vorschlag
    bleibt in seinem bisherigen Zustand.
    """
    allowed = ALLOWED_TRANSITIONS.get(proposal.state, frozenset())
    if new_state not in allowed:
        raise InvalidTransition(
            f"Übergang {proposal.state} -> {new_state} ist nicht erlaubt (Tabelle 4.2)"
        )

    from_state = proposal.state
    proposal.state = new_state
    session.add(
        ProposalEvent(
            proposal_id=proposal.id,
            from_state=from_state,
            to_state=new_state,
            actor=actor,
            reason=reason,
            log_category=log_category,
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
        log_category="board_decision",
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
