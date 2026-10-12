"""Dashboard-Lesemodell und die drei Board-Aktionen (HED-43, Entwurf HED-36 Abschnitt 5).

Dieses Modul baut keine eigene Risikoberechnung -- alle Stufe-1-Kennzahlen stehen
bereits eingefroren auf dem ``Proposal``-Datensatz (HED-41/HED-44). Was hier dazukommt,
sind nur zwei Dinge:

1. Zur Anzeige abgeleitete, *nicht* eingefrorene Felder -- Restzeit bis Verfall und der
   aktuelle (live) Zustand der zwei Sperrkennzeichen. Beide ändern sich, während der
   Vorschlag unverändert in Z3 liegt, und werden deshalb bei jeder Anfrage neu gelesen.
2. Die drei Board-Aktionen Freigeben/Ändern/Verwerfen mit serverseitiger
   Zustandsprüfung und optimistischem Lock (``Proposal.version``, siehe
   ``app.models.proposals``) gegen zwei gleichzeitige Aktionen auf denselben Vorschlag.
   Alle drei teilen sich denselben Prüf-/Protokollpfad (``_guarded_action``), damit die
   Race-Behandlung nicht dreimal leicht unterschiedlich implementiert ist.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.db import SessionFactory
from app.models import Proposal, ProposalActionAttempt
from app.models.proposals import STATE_WARTET_AUF_BOARD
from app.sperrkennzeichen import ist_firmenweit_gesperrt, ist_strategie_gesperrt
from app.trading.proposals import board_change, transition

log = logging.getLogger(__name__)


class ActionRefused(Exception):
    """Eine Board-Aktion wurde abgelehnt. ``current_state`` ist der tatsächliche,
    frisch gelesene Zustand -- für die Fehlermeldung an den Client (Plan HED-43
    Abschnitt 2: 'mit aktuellem Zustand in der Fehlermeldung, nicht nur im UI
    ausgegraut')."""

    def __init__(self, code: str, message: str, current_state: str | None = None):
        super().__init__(message)
        self.code = code
        self.current_state = current_state


@dataclass(frozen=True)
class LockState:
    firmenweit_gesperrt: bool
    strategie_gesperrt: bool


async def lock_state(session: AsyncSession, proposal: Proposal) -> LockState:
    """Feld 15: aktueller (live) Zustand beider Sperrkennzeichen. Nicht die
    eingefrorene Prüfung beim Signaleingang -- dort wurde bereits geprüft, dass das
    Signal überhaupt zu einem Proposal wurde. Hier zählt der Stand *jetzt*, damit das
    Board sieht, ob sich seit Vorschlagserstellung etwas geändert hat.

    Strategieschlüssel: ``str(proposal.strategy_id)``. Es gibt in diesem Datenmodell
    (noch) keine andere Zuordnung einer Strategie-ID zu einem Sperrkennzeichen-Schlüssel
    -- das ist eine technische Festlegung, keine Risikoentscheidung, und im PR für den
    CRO so vermerkt (offene Schnittstellenfrage 3 im Plan-Dokument).
    """
    return LockState(
        firmenweit_gesperrt=await ist_firmenweit_gesperrt(session),
        strategie_gesperrt=await ist_strategie_gesperrt(session, str(proposal.strategy_id)),
    )


def remaining_seconds(proposal: Proposal, now: datetime) -> float | None:
    if proposal.expires_at is None:
        return None
    return (proposal.expires_at - now).total_seconds()


async def list_queue(session: AsyncSession) -> list[Proposal]:
    """Alle Vorschläge, die aktuell auf eine Board-Entscheidung warten (Z3), älteste
    zuerst -- Fail-closed-Filterung (kein Platzhalter für nicht berechenbare Felder)
    ist bereits durch Stufe 1 erledigt: ein Vorschlag erreicht Z3 nur mit vollständigen
    Pflichtfeldern."""
    rows = await session.scalars(
        select(Proposal)
        .where(Proposal.state == STATE_WARTET_AUF_BOARD)
        .order_by(Proposal.signal_at)
    )
    return list(rows)


async def get_proposal(session: AsyncSession, proposal_id: int) -> Proposal | None:
    return await session.get(Proposal, proposal_id)


async def _log_attempt(
    session_factory: SessionFactory,
    proposal_id: int,
    actor: str,
    action: str,
    outcome: str,
    detail: str | None,
) -> None:
    async with session_factory() as session:
        session.add(
            ProposalActionAttempt(
                proposal_id=proposal_id, actor=actor, action=action, outcome=outcome, detail=detail
            )
        )
        await session.commit()


async def _reload_state(session_factory: SessionFactory, proposal_id: int) -> str | None:
    async with session_factory() as session:
        proposal = await session.get(Proposal, proposal_id)
        return proposal.state if proposal else None


async def _guarded_action(
    session_factory: SessionFactory,
    proposal_id: int,
    *,
    actor: str,
    action: str,
    apply: Callable[[AsyncSession, Proposal], Awaitable[Proposal]],
) -> Proposal:
    """Gemeinsamer Ablauf für Freigeben/Ändern/Verwerfen (Plan HED-43 Abschnitt 2):

    1. Vorschlag laden, serverseitig auf Z3 prüfen -- nicht nur im UI ausgegraut.
    2. ``apply`` ausführen (die eine Zustandsänderung, die die Aktion ausmacht).
    3. Committen. Hat eine zweite, gleichzeitige Aktion auf denselben Vorschlag
       zwischen Schritt 1 und hier bereits committet, schlägt der optimistische Lock
       (``Proposal.version``) fehl -- ``StaleDataError`` statt eines stillen
       Überschreibens.
    4. Jeder Ausgang -- abgelehnter Zustand, Konflikt, Erfolg -- wird protokolliert.
    """
    async with session_factory() as session:
        proposal = await session.get(Proposal, proposal_id)
        if proposal is None or proposal.state != STATE_WARTET_AUF_BOARD:
            current = proposal.state if proposal else None
            await _log_attempt(
                session_factory, proposal_id, actor, action, "rejected_state", current
            )
            raise ActionRefused(
                "not_awaiting_board",
                f"Vorschlag ist nicht in wartet_auf_board, sondern {current!r}.",
                current,
            )
        result = await apply(session, proposal)
        try:
            await session.commit()
        except StaleDataError:
            await session.rollback()
            current = await _reload_state(session_factory, proposal_id)
            await _log_attempt(
                session_factory, proposal_id, actor, action, "rejected_conflict", current
            )
            raise ActionRefused(
                "conflict",
                f"Ein anderer Entscheid wurde gleichzeitig angenommen. "
                f"Aktueller Zustand: {current!r}.",
                current,
            ) from None
        await session.refresh(result)

    # Die Zustandsänderung ist bereits committet -- ein Fehler beim Nachtrag dieses
    # Protokolleintrags darf dem Aufrufer nicht als fehlgeschlagene Aktion erscheinen
    # (sonst sieht das Board einen Fehler, obwohl freigegeben/verworfen/geändert wurde).
    try:
        await _log_attempt(session_factory, proposal_id, actor, action, "applied", result.state)
    except Exception:
        log.exception(
            "Protokolleintrag 'applied' für Vorschlag %s (%s) konnte nicht geschrieben werden",
            proposal_id,
            action,
        )
    return result


async def approve(session_factory: SessionFactory, proposal_id: int, *, actor: str) -> Proposal:
    async def apply(session: AsyncSession, proposal: Proposal) -> Proposal:
        await transition(
            session, proposal, "board_freigegeben", actor=actor, board_action="approve"
        )
        return proposal

    return await _guarded_action(
        session_factory, proposal_id, actor=actor, action="approve", apply=apply
    )


async def discard(
    session_factory: SessionFactory, proposal_id: int, *, actor: str, reason: str | None
) -> Proposal:
    async def apply(session: AsyncSession, proposal: Proposal) -> Proposal:
        await transition(
            session, proposal, "board_verworfen", actor=actor, reason=reason, board_action="discard"
        )
        return proposal

    return await _guarded_action(
        session_factory, proposal_id, actor=actor, action="discard", apply=apply
    )


async def change(
    session_factory: SessionFactory,
    proposal_id: int,
    *,
    actor: str,
    reason: str,
    size=None,
    proposal_price=None,
    stop_price=None,
) -> Proposal:
    """'Ändern' ist kein Shortcut: der alte Datensatz endet in Z6, die Fortsetzung ist
    ein neuer Vorschlag in Z0, der erneut vollständig durch Stufe 1 läuft (Plan HED-43
    Abschnitt 2 / Entwurf 5.2). Die Rückgabe ist deshalb der NEUE Datensatz, nicht der
    geänderte alte."""
    overrides = {
        k: v
        for k, v in {
            "size": size,
            "proposal_price": proposal_price,
            "stop_price": stop_price,
        }.items()
        if v is not None
    }

    async def apply(session: AsyncSession, proposal: Proposal) -> Proposal:
        return await board_change(session, proposal, actor=actor, reason=reason, **overrides)

    return await _guarded_action(
        session_factory, proposal_id, actor=actor, action="change", apply=apply
    )
