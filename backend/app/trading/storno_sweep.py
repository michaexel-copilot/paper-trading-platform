"""Storno-Sweep ruhender Einstiegsorders (Pfad 2, Entwurf HED-36 Abschnitt 9.6,
CRO-Entscheidung HED-191 Abschnitt 1).

Bewusst außerhalb ``app.kill_switch`` (1b, Option B): ``kill_switch.py`` beschreibt nur
die beiden Kennzeichen-Tabellen und ihr Protokoll, keine Handelsauswirkung (siehe
Moduldokumentation dort, Zeile 24-26). Jeder Aufrufer von ``setze_firmenweit`` /
``setze_strategie`` ruft ``sweep_ruhende_einstiegsorders`` unmittelbar danach selbst
auf -- analog der Fristwächter-Einhängung in ``app.main``.

"Ruhende Einstiegsorder" (1a, Option A): ein Proposal in Z7 "board_freigegeben" oder
Z8 "stufe2_laeuft" -- freigegeben, aber noch nicht erfolgreich an die Gegenstelle
gesendet. Kein heutiger Codepfad erzeugt eine reale, an der Börse liegende Limit-Order
(``gegenstellen_order_id`` bleibt bis zum erfolgreichen Senden ``None``); sobald sich
das ändert, ist eine echte Cancel-Fähigkeit an der Gegenstelle (1c, Option B)
Vorbedingung für den Go-Live dieses Pfads, nicht optionaler Ausbau.

Z7 läuft über den echten Versand-Treiber (``app.trading.versand_treiber.treibe_einmal``):
ein frisch gesetztes Sperrkennzeichen lässt jeden Sendeversuch sofort an
``Versandengpass._pruefe`` Schritt 3-5 scheitern -- derselbe Z7->Z9-Übergang mit
``rejection_reason_code="stufe2_abgelehnt_wegen_sperre"`` wie Pfad 18 (HED-185 Punkt 5),
nur sofort statt erst beim nächsten planmäßigen Durchlauf.

Z8 ist ein seltener Zwischenzustand (nur durch eine unterbrochene Sendung ohne
Gegenstellen-Antwort erreichbar, siehe ``versand_treiber._treibe_proposal``) und wird
von ``treibe_einmal`` nicht erfasst (das fragt ausschließlich Z7 ab). Für Z8 genügt
eine rein interne Stornierung (1c) -- es gibt nichts an der Gegenstelle zu stornieren,
solange ``gegenstellen_order_id`` nicht gesetzt ist.
"""

import logging

from sqlalchemy import select

from app.marketdata.service import MarketService
from app.models.proposals import STATE_STUFE2_ABGELEHNT, STATE_STUFE2_LAEUFT, Proposal
from app.trading.proposals import transition
from app.trading.versand_treiber import treibe_einmal
from app.trading.versandengpass import Versandengpass

log = logging.getLogger(__name__)

_STORNIERT_WEGEN_SPERRE = "storniert_wegen_sperre"


async def sweep_ruhende_einstiegsorders(
    session_factory,
    engpass: Versandengpass | None,
    *,
    market: MarketService | None = None,
    tranche: str = "B1",
) -> int:
    """Eine Durchführung des Sweeps. Gibt die Anzahl intern stornierter Z8-Vorschläge
    zurück (Tests, Protokollierung) -- die Z7-Wirkung läuft als Seiteneffekt von
    ``treibe_einmal`` und wird dort bereits gezählt/protokolliert.

    ``engpass=None`` (z. B. eine Installation ohne laufenden Matcher, F-3) heißt: kein
    Z7-Sweep möglich, nur der interne Z8-Teil läuft -- strikt konservativ, es wird
    dadurch nichts versandt, was sonst nicht auch versandt worden wäre."""
    if engpass is not None:
        await treibe_einmal(session_factory, engpass, market=market, tranche=tranche)

    betroffen = 0
    async with session_factory() as lese_session:
        proposal_ids = list(
            await lese_session.scalars(
                select(Proposal.id)
                .where(Proposal.state == STATE_STUFE2_LAEUFT)
                .order_by(Proposal.id.asc())
            )
        )
    for proposal_id in proposal_ids:
        try:
            async with session_factory() as session:
                proposal = await session.get(Proposal, proposal_id)
                if proposal is None or proposal.state != STATE_STUFE2_LAEUFT:
                    continue
                if proposal.gegenstellen_order_id is not None:
                    # 1c: keine neue Gegenstellen-Fähigkeit für normale Order -- ein
                    # Z8-Vorschlag MIT bereits gesetzter Gegenstellen-Id ist kein
                    # intern abbrechbarer Fall (sollte praktisch nicht vorkommen,
                    # siehe Moduldokumentation). Fail closed: nicht anfassen, nur
                    # protokollieren.
                    log.error(
                        "Storno-Sweep: Vorschlag %s in Z8 mit gesetzter "
                        "Gegenstellen-Id -- kein intern abbrechbarer Fall (1c), "
                        "nicht angefasst.",
                        proposal.id,
                    )
                    continue
                await transition(
                    session, proposal, STATE_STUFE2_ABGELEHNT, actor="system",
                    reason="Storno-Sweep: ruhende Einstiegsorder während aktiver "
                    "Sperre intern storniert (Pfad 2, B-3/B-15)",
                    rejection_reason_code=_STORNIERT_WEGEN_SPERRE,
                )
                await session.commit()
                betroffen += 1
        except Exception:
            log.exception("Storno-Sweep: Vorschlag %s fehlgeschlagen", proposal_id)
    return betroffen
