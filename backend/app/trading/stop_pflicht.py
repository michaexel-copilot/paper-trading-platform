"""Fill-Erkennung (Z10 -> Z11/Z12/Z13) und Pflicht-Stop-Platzierung (B-12, HED-84,
Entwurf Abschnitt 6/7/9).

Fill-Erkennung und Stop-Platzierung sitzen bewusst in einem Modul mit einem
gemeinsamen Durchlauf -- "Fill erkannt" und "Stop gesetzt" sind eine einzige Pflicht
(Veto V2), nicht zwei über zwei Intervalle verteilte Jobs (Entwurf Abschnitt 6.1).

Eskalation (Festlegung F-11): bleibt eine gefüllte Position länger als die
Pflicht-Stop-Nachfrist (``Settings.pflicht_stop_nachfrist_sekunden``) ohne
börsenseitigen Stop, wird das firmenweite Sperrkennzeichen gesetzt
(``pflicht_stop_nicht_platziert``). Das ist der eigentliche V2-Wächter für das
Zeitfenster aus Entwurf Abschnitt 1.3 -- er beseitigt das Fenster nicht, hält es aber
nicht unbegrenzt offen.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.kill_switch import setze_firmenweit
from app.models.proposals import (
    STATE_GEFUELLT,
    STATE_TEILGEFUELLT_BEENDET,
    STATE_VERSANDT,
    STATE_VON_GEGENSTELLE_ABGELEHNT,
    Proposal,
)
from app.models.stop_order import StopOrderBuch
from app.trading.proposals import transition
from app.trading.versandengpass import Versandengpass, VersandOrder

log = logging.getLogger(__name__)

_PFLICHT_STOP_AUSLOESER = "pflicht_stop_nicht_platziert"


async def pruefe_fills_einmal(
    session_factory,
    engpass: Versandengpass,
    *,
    exchange: str = "okx",
    pflicht_stop_nachfrist: timedelta,
) -> int:
    """Eine Durchführung (Entwurf Abschnitt 6.2). Gibt die Anzahl der in diesem
    Durchlauf bearbeiteten Vorschläge zurück (Tests, Protokollierung).

    Eigene Session je Vorschlag -- gleiche Begründung wie in
    ``app.trading.versand_treiber.treibe_einmal``.
    """
    async with session_factory() as lese_session:
        proposal_ids = list(
            await lese_session.scalars(
                select(Proposal.id)
                .where(Proposal.state == STATE_VERSANDT)
                .order_by(Proposal.id.asc())
            )
        )

    bearbeitet = 0
    for proposal_id in proposal_ids:
        try:
            async with session_factory() as session:
                proposal = await session.get(Proposal, proposal_id)
                if proposal is None:
                    continue
                await _bearbeite_proposal(
                    session, proposal, exchange, engpass, pflicht_stop_nachfrist
                )
                bearbeitet += 1
        except Exception:
            log.exception("Fill-Erkennung: Vorschlag %s fehlgeschlagen", proposal_id)
    return bearbeitet


async def _bearbeite_proposal(
    session: AsyncSession,
    proposal: Proposal,
    exchange: str,
    engpass: Versandengpass,
    pflicht_stop_nachfrist: timedelta,
) -> None:
    if proposal.gegenstellen_order_id is None:
        log.error(
            "Fill-Erkennung: Vorschlag %s in Z10 ohne gegenstellen_order_id -- "
            "Status nicht abfragbar.",
            proposal.id,
        )
        return

    symbol = await _boersensymbol_fuer_proposal(session, proposal, exchange)
    if symbol is None:
        log.error(
            "Fill-Erkennung: kein Börsensymbol für Vorschlag %s -- Status nicht "
            "abfragbar.",
            proposal.id,
        )
        return

    # Eskalationsprüfung (F-11, siehe unten/_pruefe_eskalation) läuft auf JEDEM
    # Durchlauf, unabhängig davon, ob dieser Durchlauf selbst etwas Neues feststellt --
    # auch wenn die Stop-Platzierung weiter unten fehlschlägt oder der Status gerade
    # nicht lesbar ist, darf eine seit der Nachfrist ungedeckte Position nicht
    # unbemerkt bleiben.
    status = await engpass.lese_order_status(exchange, proposal.gegenstellen_order_id, symbol)
    if status is None:
        # Entwurf Tabelle 6.2, letzte Zeile: nicht lesbar -- kein stiller
        # Zwischenzustand, nur ein erneuter Versuch im nächsten Durchlauf.
        await _pruefe_eskalation(session, proposal, pflicht_stop_nachfrist)
        await session.commit()
        return

    if status.status == "offen":
        # Noch keine Füllung -- nichts zu schützen, kein Zustandswechsel, keine
        # Eskalation möglich (gefuellte_menge ist 0).
        return

    bisherige_menge = proposal.gefuellte_menge or Decimal(0)
    neue_menge = status.gefuellte_menge

    if status.status == "abgelehnt" or (status.status == "beendet" and neue_menge <= 0):
        await transition(
            session, proposal, STATE_VON_GEGENSTELLE_ABGELEHNT, actor="system",
            reason="Fill-Erkennung: Einstiegs-Order von der Gegenstelle abgelehnt/"
            "beendet ohne Füllung (B-12)",
        )
        await session.commit()
        return

    if neue_menge > bisherige_menge:
        if proposal.filled_at is None:
            # Zeitpunkt der ERSTEN Füllung, nicht des Erkennens eines Endzustands --
            # das ist die Uhr, gegen die F-11 die Pflicht-Stop-Nachfrist misst (eine
            # lange offene Teilfüllung darf die Eskalation nicht aufschieben, nur weil
            # die Order noch nicht beendet ist).
            proposal.filled_at = utcnow()
        proposal.gefuellte_menge = neue_menge
        await _stelle_stop_sicher(session, proposal, symbol, exchange, engpass, neue_menge)

    if status.status == "gefuellt":
        await transition(
            session, proposal, STATE_GEFUELLT, actor="system",
            reason="Fill-Erkennung: Einstiegs-Order vollständig gefüllt (B-12)",
        )
    elif status.status == "beendet":
        # teilgefüllt UND von der Gegenstelle beendet (Entwurf Tabelle 6.2, Zeile 2).
        await transition(
            session, proposal, STATE_TEILGEFUELLT_BEENDET, actor="system",
            reason="Fill-Erkennung: Einstiegs-Order teilgefüllt und von der "
            "Gegenstelle beendet (storniert/abgelaufen, B-12)",
        )
    # status == "teilgefuellt": bleibt Z10, Stop wurde oben bereits für die
    # Teilmenge sichergestellt (Entwurf Tabelle 6.2, Zeile 4).
    await _pruefe_eskalation(session, proposal, pflicht_stop_nachfrist)
    await session.commit()


async def _boersensymbol_fuer_proposal(
    session: AsyncSession, proposal: Proposal, exchange: str
) -> str | None:
    from app.models import Asset, AssetSourceSymbol

    asset = await session.scalar(select(Asset).where(Asset.symbol == proposal.instrument))
    if asset is None:
        return None
    return await session.scalar(
        select(AssetSourceSymbol.symbol).where(
            AssetSourceSymbol.asset_id == asset.id, AssetSourceSymbol.source == exchange
        )
    )


async def _stelle_stop_sicher(
    session: AsyncSession,
    proposal: Proposal,
    symbol: str,
    exchange: str,
    engpass: Versandengpass,
    gefuellte_menge: Decimal,
) -> None:
    """Festlegung F-7/F-13: Stop über die gesamte bisher gefüllte Menge sicherstellen.

    Noch kein eigener offener Stop -> neu platzieren (Abschnitt 7.1). Bereits ein
    offener Stop mit kleinerer Menge (Teilfüllung Nummer zwei oder höher) -> Nachführung
    nach F-13: neuer Stop zuerst, alter danach stornieren (Abschnitt 9.3).
    """
    bestehender = await session.scalar(
        select(StopOrderBuch).where(
            StopOrderBuch.anlass_proposal_id == proposal.id,
            StopOrderBuch.zustand == "offen",
        )
    )
    if bestehender is not None and bestehender.menge_soll >= gefuellte_menge:
        return  # bereits ausreichend gedeckt

    stop_side = "sell" if proposal.direction == "long" else "buy"
    order = VersandOrder(
        gattung="stop_platzierung",
        strategie_schluessel=str(proposal.strategy_id),
        exchange=exchange,
        symbol=symbol,
        side=stop_side,
        quantity=gefuellte_menge,
        order_type="stop_market",
        stop_price=proposal.stop_price,
        anlass_proposal_id=proposal.id,
    )
    ergebnis = await engpass.send(session, order)
    if not ergebnis.angenommen:
        # F-11: kein Stop zustande gekommen -- bleibt ungedeckt, Eskalation entscheidet
        # über die Nachfrist (siehe _pruefe_eskalation, vom Aufrufer separat geprüft).
        log.error(
            "Stop-Platzierung für Vorschlag %s abgelehnt (%s) -- Position bleibt bis "
            "zur nächsten Nachfrist-Prüfung ungedeckt.",
            proposal.id,
            ergebnis.ablehnungsgrund,
        )
        return

    neuer_eintrag = StopOrderBuch(
        strategie_schluessel=str(proposal.strategy_id),
        exchange=exchange,
        symbol=symbol,
        side=stop_side,
        menge_soll=gefuellte_menge,
        trigger_soll=proposal.stop_price,
        gegenstellen_algo_id=ergebnis.gegenstellen_order_id,
        anlass_proposal_id=proposal.id,
        zustand="offen",
    )
    session.add(neuer_eintrag)
    await session.flush()  # neuer_eintrag.id wird für ersetzt_durch_id gebraucht

    if bestehender is not None:
        # F-13: alter Stop erst NACH dem erfolgreich platzierten neuen Stop
        # stornieren -- kurzzeitige Doppeldeckung statt eines Fensters ganz ohne Stop.
        bestehender.zustand = "nachfuehrung_laeuft"
        await session.flush()
        storno_order = VersandOrder(
            gattung="stornierung",
            strategie_schluessel=str(proposal.strategy_id),
            exchange=exchange,
            symbol=symbol,
            side=stop_side,
            quantity=bestehender.menge_soll,
            order_type="stornierung",
            anlass_proposal_id=proposal.id,
            ziel_order_id=bestehender.gegenstellen_algo_id,
        )
        storno_ergebnis = await engpass.storniere(session, storno_order)
        if storno_ergebnis.angenommen:
            bestehender.zustand = "ersetzt"
            bestehender.beendet_at = utcnow()
            bestehender.ersetzt_durch_id = neuer_eintrag.id
        else:
            # Scheitert die Stornierung, bleibt der alte Stop als
            # "nachfuehrung_laeuft" liegen -- der tägliche Abgleich behandelt das als
            # Toleranzfall (Entwurf 10.3, Punkt 6), keine sofortige Eskalation hier.
            log.error(
                "Stornierung des alten Stops für Vorschlag %s fehlgeschlagen (%s).",
                proposal.id,
                storno_ergebnis.ablehnungsgrund,
            )


async def _pruefe_eskalation(
    session: AsyncSession, proposal: Proposal, pflicht_stop_nachfrist: timedelta
) -> None:
    """Festlegung F-11: gefüllte Position länger als die Nachfrist ohne
    börsenseitigen Stop -> Kill-Switch Stufe 2."""
    gefuellte_menge = proposal.gefuellte_menge or Decimal(0)
    if gefuellte_menge <= 0:
        return
    hat_offenen_stop = (
        await session.scalar(
            select(StopOrderBuch.id).where(
                StopOrderBuch.anlass_proposal_id == proposal.id,
                StopOrderBuch.zustand.in_(("offen", "nachfuehrung_laeuft")),
            )
        )
    ) is not None
    if hat_offenen_stop:
        return
    if proposal.filled_at is None:
        return
    if utcnow() - proposal.filled_at <= pflicht_stop_nachfrist:
        return
    await setze_firmenweit(
        session,
        ausloeser=_PFLICHT_STOP_AUSLOESER,
        begruendung=(
            f"Pflicht-Stop für Vorschlag {proposal.id} seit mehr als "
            f"{pflicht_stop_nachfrist} ohne börsenseitige Deckung (B-12, Festlegung "
            "F-11)."
        ),
    )
