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

Fill-Buchung (HED-109, Entwurf Abschnitt 4): jeder Zuwachs der gefüllten Menge wird in
``Position``/``Portfolio`` gebucht (``_buche_einstiegsfill``, dieselbe Logik wie der
alte Dashboard-Matcher in ``engine.try_fill`` über ``app.trading.fill_buchung``), bevor
der Zustand fortgeschrieben wird. Nur die Einstiegsseite (F-4: ausschließlich
``side == "buy"`` kommt über diese Pipeline vor) -- die Exit-Seite (Stop-Fills,
``auto_exit``-Fills) ist ein eigener, noch offener Nebenbefund.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.kill_switch import setze_firmenweit
from app.marketdata.service import MarketService
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

_OFFENE_STOP_ZUSTAENDE = ("offen", "nachfuehrung_laeuft")

log = logging.getLogger(__name__)

_PFLICHT_STOP_AUSLOESER = "pflicht_stop_nicht_platziert"


async def pruefe_fills_einmal(
    session_factory,
    engpass: Versandengpass,
    market: MarketService,
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
                    session, proposal, exchange, engpass, market, pflicht_stop_nachfrist
                )
                bearbeitet += 1
        except Exception:
            log.exception("Fill-Erkennung: Vorschlag %s fehlgeschlagen", proposal_id)

    # E-12 (R-5, Standardannahme Fassung 1.0, CRO-Nachforderung HED-107): ein Stop zu
    # einer bereits von `auto_exit.py` geschlossenen Position wird hier abgeräumt --
    # `auto_exit.py` selbst bleibt unverändert. Läuft über ALLE offenen Stops, nicht
    # nur über Vorschläge in Z10 (STATE_VERSANDT) wie oben, weil ein Stop auch nach
    # Erreichen von Z11/Z12 (GEFUELLT/TEILGEFUELLT_BEENDET) noch offen sein kann.
    async with session_factory() as lese_session:
        stop_ids = list(
            await lese_session.scalars(
                select(StopOrderBuch.id).where(
                    StopOrderBuch.zustand.in_(_OFFENE_STOP_ZUSTAENDE),
                    StopOrderBuch.anlass_proposal_id.is_not(None),
                )
            )
        )

    for stop_id in stop_ids:
        try:
            async with session_factory() as session:
                stop = await session.get(StopOrderBuch, stop_id)
                if stop is None or stop.zustand not in _OFFENE_STOP_ZUSTAENDE:
                    continue
                await _raeume_stop_bei_geschlossener_position(session, stop, engpass)
                await session.commit()
        except Exception:
            log.exception("Abräumen geschlossener Stops: Stop %s fehlgeschlagen", stop_id)

    return bearbeitet


async def _bearbeite_proposal(
    session: AsyncSession,
    proposal: Proposal,
    exchange: str,
    engpass: Versandengpass,
    market: MarketService,
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
        gebucht = await _buche_einstiegsfill(
            session, market, proposal, neue_menge - bisherige_menge
        )
        if not gebucht:
            # HED-109 Entwurf Abschnitt 4.2: kein stiller Teilerfolg -- Portfolio oder
            # Asset nicht auflösbar heißt NICHT gebucht, NICHT fortgeschaltet, kein
            # Stop. Fehler ist bereits in `_buche_einstiegsfill` protokolliert; der
            # nächste Durchlauf versucht es erneut (gleiches Muster wie beim nicht
            # lesbaren Status oben).
            await session.commit()
            return
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


async def _buche_einstiegsfill(
    session: AsyncSession, market: MarketService, proposal: Proposal, delta_menge: Decimal
) -> bool:
    """HED-109 (Entwurf Abschnitt 4.2/4.3/4.4): bucht den Zuwachs einer
    Einstiegs-Füllung in Position/Cash/Gebühren -- dieselbe Logik, die der alte
    Dashboard-Matcher für Orders benutzt (``app.trading.fill_buchung.buche_fill``), hier
    zum ersten Mal auch für die Proposal-Pipeline.

    Nur Käufe: F-4 (``versand_treiber._treibe_proposal``) sendet über diese Pipeline
    ausschließlich ``side == "buy"``/``direction == "long"`` -- die Exit-Seite ist ein
    eigener, hier ausdrücklich nicht behandelter Nebenbefund (Entwurf Abschnitt 3).

    Preisquelle ist ``proposal.limit_price``, nicht ein Börsenkurs (Entwurf Abschnitt
    4.3, gleiche Linie wie der B-12-Stop-Kurs) -- ein Limit-Buy füllt nie teurer als
    sein Limit, die Buchung kann die Kosten also nur über-, nie unterschätzen.
    Liquiditätsannahme fest "taker" (Entwurf Abschnitt 4.4): OKX-Demo liefert kein
    maker/taker-Flag, "taker" ist die höhere, damit konservative Gebühr.

    Fail closed, drei Stufen (Strategie ohne Portfolio, Instrument nicht im Katalog,
    Portfolio-Zeile fehlt): gibt False zurück und bucht nichts. Der Aufrufer darf dann
    weder den Zustand noch ``gefuellte_menge`` fortschreiben (kein stiller
    Teilerfolg) -- derselbe Fail-closed-Grundsatz wie in ``_position_noch_offen``.
    """
    from app.models import Asset, Portfolio, Strategy
    from app.trading.engine import amounts
    from app.trading.fill_buchung import buche_fill
    from app.trading.orders import pricing_for

    strategy = await session.get(Strategy, proposal.strategy_id)
    if strategy is None or strategy.portfolio_id is None:
        log.error(
            "Fill-Buchung: Vorschlag %s ohne aufgelöstes Portfolio -- nicht gebucht, "
            "Zustand bleibt unverändert (HED-109).",
            proposal.id,
        )
        return False
    asset = await session.scalar(select(Asset).where(Asset.symbol == proposal.instrument))
    if asset is None:
        log.error(
            "Fill-Buchung: Instrument %s von Vorschlag %s nicht im Katalog -- nicht "
            "gebucht, Zustand bleibt unverändert (HED-109).",
            proposal.instrument,
            proposal.id,
        )
        return False
    portfolio = await session.scalar(
        select(Portfolio).where(Portfolio.id == strategy.portfolio_id).with_for_update()
    )
    if portfolio is None:
        log.error(
            "Fill-Buchung: Portfolio %s von Vorschlag %s existiert nicht -- nicht "
            "gebucht, Zustand bleibt unverändert (HED-109).",
            strategy.portfolio_id,
            proposal.id,
        )
        return False

    pricing = await pricing_for(session, market, portfolio, asset, view=None)
    result = amounts("buy", proposal.limit_price, delta_menge, "taker", pricing)
    await buche_fill(
        session, portfolio, asset.id, "buy", proposal.limit_price, delta_menge,
        pricing.fx_rate, result,
    )
    return True


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


async def _position_noch_offen(session: AsyncSession, proposal: Proposal) -> bool:
    """E-12 (R-5): dieselbe Positionsquelle wie ``app.trading.auto_exit._offene_position``
    -- das eigene Buch (``app.models.portfolios.Position``).

    Fail closed, drei Stufen: Strategie ohne Portfolio, Instrument nicht im Katalog,
    oder **keine Buchzeile vorhanden** -> gilt als weiterhin offen, Stop bleibt stehen.
    Der dritte Fall ist kein hypothetischer Rand: die Proposal-Pipeline
    (``versand_treiber``/``stop_pflicht``) bucht gefüllte Mengen nirgends in
    ``Position`` -- das tut ausschließlich der alte Dashboard-Matcher
    (``app.trading.engine.try_fill``). Ohne diese Stufe würde JEDER frische Fill
    dieser Pipeline sofort als „Position geschlossen" gelesen (keine Zeile ->
    Menge 0) und sein Pflicht-Stop im nächsten Durchlauf wieder storniert --
    das Gegenteil von fail closed. Sobald eine Buchzeile existiert, ist ihr
    tatsächlicher Bestand die Wahrheit (auch wenn er 0 ist)."""
    from app.models import Asset, Position, Strategy

    strategy = await session.get(Strategy, proposal.strategy_id)
    if strategy is None or strategy.portfolio_id is None:
        return True
    asset = await session.scalar(select(Asset).where(Asset.symbol == proposal.instrument))
    if asset is None:
        return True
    position = await session.scalar(
        select(Position).where(
            Position.portfolio_id == strategy.portfolio_id, Position.asset_id == asset.id
        )
    )
    if position is None:
        return True
    return position.quantity > 0 if proposal.direction == "long" else position.quantity < 0


async def _raeume_stop_bei_geschlossener_position(
    session: AsyncSession, stop: StopOrderBuch, engpass: Versandengpass
) -> None:
    """E-12 (R-5, Standardannahme Fassung 1.0): Stop zu einer bereits geschlossenen
    Position automatisch über ``gattung="stornierung"`` abräumen -- sonst meldet der
    tägliche Abgleich (``app.trading.stop_orderbuch_abgleich``) beim nächsten
    regulären strategiegetriebenen Ausstieg (``auto_exit.py``) eine unechte Waise
    oder Mengenabweichung und löst einen unechten Kill-Switch-Stufe-2-Alarm aus.

    Nur die binäre Frage "noch offen oder nicht" wird geprüft (kein Verringern der
    Stop-Menge bei Teil-Ausstieg) -- bewusste Scope-Begrenzung aus Entwurf Abschnitt
    14, R-5/E-12."""
    if stop.anlass_proposal_id is None:
        return
    proposal = await session.get(Proposal, stop.anlass_proposal_id)
    if proposal is None:
        return
    if await _position_noch_offen(session, proposal):
        return

    storno_order = VersandOrder(
        gattung="stornierung",
        strategie_schluessel=stop.strategie_schluessel,
        exchange=stop.exchange,
        symbol=stop.symbol,
        side=stop.side,
        quantity=stop.menge_soll,
        order_type="stornierung",
        anlass_proposal_id=proposal.id,
        ziel_order_id=stop.gegenstellen_algo_id,
    )
    ergebnis = await engpass.storniere(session, storno_order)
    if ergebnis.angenommen:
        stop.zustand = "storniert"
        stop.beendet_at = utcnow()
    else:
        # Scheitert die Stornierung, bleibt der Stop offen -- der nächste Durchlauf
        # versucht es erneut; bis dahin behandelt der tägliche Abgleich ihn ggf. als
        # Waise (gleiche Toleranzbehandlung wie bei einer gescheiterten Nachführung).
        log.error(
            "Abräumen des Stops %s zu geschlossener Position (Vorschlag %s) "
            "fehlgeschlagen (%s) -- bleibt bis zum nächsten Durchlauf offen.",
            stop.id,
            proposal.id,
            ergebnis.ablehnungsgrund,
        )
