"""B-2 Stufe 1 -- Vorfilter vor Anzeige im Dashboard (Entwurf HED-36 Abschnitt 6.1,
CRO-freigegeben).

Nimmt einen frisch angelegten Vorschlag in Z0 (``app.trading.proposals.create_proposal``)
und führt ihn über Z1 nach Z2 (abgelehnt) oder Z3 (wartet auf Board).

Was dieses Modul NICHT tut (Abgrenzung, Entwurf Abschnitt 2):
- Die Doppelprüfung der Sperrkennzeichen (Schritte vor 1.1) -- die läuft vor dem
  Aufruf von :func:`lauf_stufe1`, beim Entstehen des Signals, nicht hier. Ein
  gesetztes Kennzeichen bedeutet, dass gar kein Proposal-Datensatz angelegt wird
  (``unterdrueckt_durch_sperre``, Entwurf 6.1, Abschnitt 9.2) -- dieses Modul sieht
  solche Signale nie.
- Das Dashboard selbst (Anzeige, Board-Aktionen) -- Entwurf Abschnitt 5.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.marketdata.base import DataUnavailable
from app.marketdata.service import MarketService
from app.models import Asset, Order, Portfolio, Proposal, Strategy, ValueSnapshot
from app.models.proposals import (
    STATE_STUFE1_ABGELEHNT,
    STATE_STUFE1_LAEUFT,
    STATE_WARTET_AUF_BOARD,
)
from app.preisband import berechne_grenzpreis
from app.risikolimits import LimitErgebnis, RisikoKontext, pruefe_alle_limits, strengerer_wert
from app.trading.proposals import transition
from app.trading.valuation import Valuation, value_portfolio

PROPOSAL_VALIDITY_AB_BOARD_FREIGABE = timedelta(seconds=60)  # Entwurf 6.3, Gültigkeitsdauer


class Stufe1Fehler(ValueError):
    """Eingabedaten so unvollständig, dass selbst eine Ablehnung nicht sauber gebildet
    werden kann (z. B. Strategie ohne Portfolio-Verknüpfung) -- wird vom Aufrufer als
    Z2 mit diesem Grund behandelt, siehe :func:`lauf_stufe1`."""


async def _letzter_wert_vor(
    session: AsyncSession, portfolio_id: int, grenze: datetime
) -> Decimal | None:
    return await session.scalar(
        select(ValueSnapshot.value)
        .where(ValueSnapshot.portfolio_id == portfolio_id, ValueSnapshot.at < grenze)
        .order_by(ValueSnapshot.at.desc())
        .limit(1)
    )


async def _hoechststand(session: AsyncSession, portfolio_id: int) -> Decimal | None:
    return await session.scalar(
        select(func.max(ValueSnapshot.value)).where(ValueSnapshot.portfolio_id == portfolio_id)
    )


async def _offene_stop_preise(
    session: AsyncSession, portfolio_id: int, asset_ids: list[int]
) -> dict[int, Decimal]:
    """Je Asset die erste offene börsenseitige Stop-Order (type=stop, status=open).

    Fehlt eine, trägt das zurückgegebene dict den Asset nicht -- der Aufrufer
    (``_summenrisiko_offener_positionen``) behandelt das als "diese Position hat
    keinen auslesbaren Stop" und macht das Summenrisiko fail-closed nicht berechenbar
    (HED-8 Tabelle 4.2: "Position ohne börsenseitigen Stop entdeckt" ist selbst ein
    eigener Kill-Switch-Auslöser, B-12 -- hier nur die Konsequenz für Stufe 1).
    """
    if not asset_ids:
        return {}
    rows = await session.execute(
        select(Order.asset_id, Order.stop_price).where(
            Order.portfolio_id == portfolio_id,
            Order.asset_id.in_(asset_ids),
            Order.type == "stop",
            Order.status == "open",
        )
    )
    ergebnis: dict[int, Decimal] = {}
    for asset_id, stop_price in rows:
        ergebnis.setdefault(asset_id, stop_price)
    return ergebnis


def _summenrisiko_offener_positionen(
    valuation: Valuation, stop_preise: dict[int, Decimal]
) -> Decimal | None:
    gesamt = Decimal(0)
    for position in valuation.positions:
        if position.quantity == 0:
            continue
        stop_price = stop_preise.get(position.asset.id)
        if stop_price is None or position.mark.price_base is None:
            return None
        gesamt += position.quantity * abs(position.mark.price_base - stop_price)
    return gesamt


@dataclass(frozen=True, slots=True)
class ProposalEingabe:
    """Die Felder aus dem Z0-Vorschlag, die Stufe 1 zur Berechnung braucht."""

    instrument_asset_id: int
    direction: str  # long | short
    size: Decimal
    proposal_price: Decimal
    stop_price: Decimal
    stop_distance: Decimal
    stop_distance_pct: Decimal
    proposal_kind: str  # entry | increase


async def sammle_kontext(
    session: AsyncSession,
    market: MarketService,
    portfolio: Portfolio,
    eingabe: ProposalEingabe,
    *,
    tranche: str,
    tranche_eur: Decimal,
) -> RisikoKontext:
    """Baut den :class:`RisikoKontext` aus dem aktuellen Stand von Buch und Markt.

    Wird sowohl von Stufe 1 (jetzt) als auch von Stufe 2 (unmittelbar vor dem Senden,
    also später mit zwangsläufig anderen Werten) aufgerufen -- derselbe Code, nicht
    nur dieselbe Formel (Festlegung 6.1.1).
    """
    valuation = await value_portfolio(session, market, portfolio)
    nav = valuation.total_value

    now = market.now()
    day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    month_start = day_start.replace(day=1)

    nav_hoechststand = await _hoechststand(session, portfolio.id)
    if nav_hoechststand is not None:
        nav_hoechststand = max(nav_hoechststand, nav)
    nav_tagesanfang = await _letzter_wert_vor(session, portfolio.id, day_start)
    nav_vor_7_tagen = await _letzter_wert_vor(session, portfolio.id, now - timedelta(days=7))
    nav_monatsanfang = await _letzter_wert_vor(session, portfolio.id, month_start)

    asset = await session.get(Asset, eingabe.instrument_asset_id)
    if asset is None:
        raise Stufe1Fehler(f"Asset {eingabe.instrument_asset_id} nicht im Katalog")

    try:
        fx_rate = (await market.fx(asset.quote_currency, portfolio.base_currency)).rate
    except DataUnavailable:
        fx_rate = None

    risiko_je_trade_eur = (eingabe.size * eingabe.stop_distance * fx_rate
                           if fx_rate is not None else None)

    bestehende_position = next(
        (p for p in valuation.positions if p.asset.id == eingabe.instrument_asset_id), None
    )
    bestehender_marktwert = bestehende_position.market_value if bestehende_position else Decimal(0)
    neue_notional = (eingabe.size * eingabe.proposal_price * fx_rate
                     if fx_rate is not None else None)
    einzelwert_marktwert_nach_trade = (
        bestehender_marktwert + neue_notional if neue_notional is not None else None
    )

    holdings_value_nach_trade = sum(
        (p.market_value for p in valuation.positions), start=Decimal(0)
    )
    brutto_exposure = (
        holdings_value_nach_trade + neue_notional if neue_notional is not None else None
    )

    anzahl_offener_positionen = sum(1 for p in valuation.positions if p.quantity != 0)
    ist_neues_instrument = bestehende_position is None or bestehende_position.quantity == 0
    if eingabe.proposal_kind == "entry" and ist_neues_instrument:
        anzahl_offener_positionen += 1

    asset_ids = [p.asset.id for p in valuation.positions if p.quantity != 0]
    stop_preise = await _offene_stop_preise(session, portfolio.id, asset_ids)
    summenrisiko_vor_trade = _summenrisiko_offener_positionen(valuation, stop_preise)
    summenrisiko_nach_trade = (
        summenrisiko_vor_trade + risiko_je_trade_eur
        if summenrisiko_vor_trade is not None and risiko_je_trade_eur is not None
        else None
    )

    hat_offene_positionen = anzahl_offener_positionen > 0
    # Korrelation ist nur dann 0 (trivial, keine Gruppe), wenn es VOR diesem Trade
    # bereits keine offenen Positionen gab -- siehe app.risikolimits für die
    # Begründung, warum eine echte Berechnung mit offenen Positionen heute fehlt.
    korrelation = None if hat_offene_positionen else Decimal(0)

    risikobudget_grenze = strengerer_wert(Decimal("0.005"), nav, Decimal("0.02"), tranche_eur)
    # Die Formel in HED-8 Tabelle 4.2 bezieht sich auf Risikobudget (Euro) /
    # (1,5 x Stop-Abstand in Prozent) und ergibt eine *Positionsgröße in Euro*, nicht
    # in Stück -- zum Vergleich mit eingabe.size (Stück) wird daher die Obergrenze in
    # Stück umgerechnet (/ proposal_price, FX-bereinigt).
    positionsgroesse_implizierte_obergrenze = None
    if eingabe.stop_distance_pct > 0 and fx_rate is not None and eingabe.proposal_price > 0:
        obergrenze_eur = risikobudget_grenze / (Decimal("1.5") * eingabe.stop_distance_pct)
        positionsgroesse_implizierte_obergrenze = (
            obergrenze_eur / (eingabe.proposal_price * fx_rate)
        )

    return RisikoKontext(
        tranche=tranche,
        tranche_eur=tranche_eur,
        nav=nav,
        direction=eingabe.direction,
        nav_hoechststand=nav_hoechststand,
        nav_tagesanfang=nav_tagesanfang,
        nav_vor_7_tagen=nav_vor_7_tagen,
        nav_monatsanfang=nav_monatsanfang,
        cash=valuation.cash,
        brutto_exposure=brutto_exposure,
        anzahl_offener_positionen=anzahl_offener_positionen,
        einzelwert_marktwert_nach_trade=einzelwert_marktwert_nach_trade,
        summenrisiko_vor_trade=summenrisiko_vor_trade,
        summenrisiko_nach_trade=summenrisiko_nach_trade,
        korrelation_zu_offenen_positionen=korrelation,
        hat_offene_positionen=hat_offene_positionen,
        risiko_je_trade_eur=risiko_je_trade_eur,
        positionsgroesse_implizierte_obergrenze=positionsgroesse_implizierte_obergrenze,
        proposal_size=eingabe.size,
    )


def _erster_bruch(ergebnisse: list[LimitErgebnis]) -> LimitErgebnis | None:
    return next((e for e in ergebnisse if e.gebrochen), None)


async def lauf_stufe1(
    session: AsyncSession,
    market: MarketService,
    strategy: Strategy,
    proposal: Proposal,
    eingabe: ProposalEingabe,
    *,
    tranche: str,
    tranche_eur: Decimal,
) -> Proposal:
    """Schritte 1.1-1.13 in Reihenfolge, fail closed (Festlegung 6.1.3).

    Löst das Portfolio über ``strategy.portfolio_id`` selbst auf (wie
    ``app.trading.stufe2.pruefe_stufe2`` es für Stufe 2 tut) -- eine Strategie ohne
    Buch kann so keinen Vorschlag durch Stufe 1 bringen, unabhängig davon, wer
    dieses Modul künftig aufruft.

    Bricht beim ersten harten Fehler ab, protokolliert aber den Stand aller bis
    dahin berechneten Kennzahlen (Festlegung 6.1.2) -- hier technisch: Stufe 1 läuft
    IMMER alle Limits der Katalog-Funktion durch (billige, reine Berechnung) und
    wählt danach den ersten Bruch für die Ablehnungsbegründung aus; das Ergebnis ist
    identisch zu einem echten Abbruch beim ersten Fehler, weil jede spätere Prüfung
    ohnehin verworfen würde -- nur der gespeicherte ``limit_utilization``-Schnappschuss
    zeigt zusätzlich ehrlich, was *sonst* auch schon gebrochen war.
    """
    await transition(session, proposal, STATE_STUFE1_LAEUFT, actor="system",
                     reason="Stufe 1 gestartet")

    portfolio = (
        await session.get(Portfolio, strategy.portfolio_id) if strategy.portfolio_id else None
    )
    if portfolio is None:
        await transition(
            session, proposal, STATE_STUFE1_ABGELEHNT, actor="system",
            reason="Strategie ohne Portfolio-Verknüpfung -- Stufe 1 nicht berechenbar",
            rejection_reason_code="kein_verknuepftes_portfolio",
        )
        return proposal

    try:
        ctx = await sammle_kontext(session, market, portfolio, eingabe, tranche=tranche,
                                   tranche_eur=tranche_eur)
    except Stufe1Fehler as exc:
        await transition(
            session, proposal, STATE_STUFE1_ABGELEHNT, actor="system",
            reason=str(exc), rejection_reason_code="kontext_nicht_bildbar",
        )
        return proposal

    ergebnisse = pruefe_alle_limits(ctx)

    proposal.nav = ctx.nav
    proposal.drawdown = (
        (ctx.nav_hoechststand - ctx.nav) if ctx.nav_hoechststand is not None else None
    )
    proposal.aggregate_risk_utilization_before = ctx.summenrisiko_vor_trade
    proposal.aggregate_risk_utilization_after = ctx.summenrisiko_nach_trade
    proposal.risk_eur = ctx.risiko_je_trade_eur
    proposal.correlation_to_open_positions = ctx.korrelation_zu_offenen_positionen
    proposal.limit_utilization = [e.to_json() for e in ergebnisse]

    bruch = _erster_bruch(ergebnisse)
    if bruch is not None:
        await transition(
            session, proposal, STATE_STUFE1_ABGELEHNT, actor="system",
            reason=bruch.begruendung, rejection_reason_code=bruch.limit,
        )
        return proposal

    # Grenzpreis einfrieren (Abschnitt 6.3): das Dashboard muss "Freigeben bis
    # maximal X" zeigen können, bevor das Board entscheidet -- nicht erst in Stufe 2.
    proposal.limit_price = berechne_grenzpreis(
        eingabe.direction, eingabe.proposal_price, eingabe.stop_price, eingabe.stop_distance_pct
    )

    await transition(session, proposal, STATE_WARTET_AUF_BOARD, actor="system",
                     reason="Stufe 1 bestanden, alle Limits eingehalten")
    return proposal
