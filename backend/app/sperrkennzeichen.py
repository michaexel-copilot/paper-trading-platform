"""Fail-closed Lesezugriff auf die zwei Sperrkennzeichen (Entwurf Abschnitt 9.2).

Prüfreihenfolge ist verbindlich und immer gleich: firmenweit zuerst, dann
strategiebezogen. Jeder Lesefehler -- Verbindungsfehler, unerwarteter Zustand -- gilt
als 'gesetzt'. Es gibt keinen Parameter, der das abschaltet.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen

log = logging.getLogger(__name__)


async def ist_firmenweit_gesperrt(session: AsyncSession) -> bool:
    try:
        zeile = await session.scalar(select(FirmenweitesSperrkennzeichen).limit(1))
    except Exception:
        log.exception("firmenweites Sperrkennzeichen nicht lesbar -- fail closed, gesetzt")
        return True
    if zeile is None:
        log.error("firmenweites Sperrkennzeichen nicht vorhanden -- fail closed, gesetzt")
        return True
    return zeile.gesetzt


async def ist_strategie_gesperrt(session: AsyncSession, strategie_schluessel: str) -> bool:
    try:
        zeile = await session.scalar(
            select(StrategieSperrkennzeichen).where(
                StrategieSperrkennzeichen.strategie_schluessel == strategie_schluessel
            )
        )
    except Exception:
        log.exception(
            "Strategie-Sperrkennzeichen für %s nicht lesbar -- fail closed, gesetzt",
            strategie_schluessel,
        )
        return True
    if zeile is None:
        # Kein Lesefehler, sondern ein bestätigtes "noch nie gesetzt" -- Lückenfall
        # Abschnitt 9.4: eine neue Strategie startet offen, nicht gesperrt.
        return False
    return zeile.gesetzt


async def doppelpruefung_offen(session: AsyncSession, strategie_schluessel: str) -> bool:
    """True nur, wenn BEIDE Kennzeichen offen sind. Firmenweit wird zuerst gelesen."""
    if await ist_firmenweit_gesperrt(session):
        return False
    if await ist_strategie_gesperrt(session, strategie_schluessel):
        return False
    return True
