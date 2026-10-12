"""B-2 Stufe 2 -- Nachprüfung unmittelbar vor dem Senden (Entwurf HED-36 Abschnitt 6.2,
CRO-freigegeben). Wird in die zwei fail-closed Einhängepunkte des Versand-Engpasses
(HED-42, ``app.trading.versandengpass.Versandengpass``) eingesetzt:
``freigabe_nachweis_pruefer`` (Schritte 2.2/2.3) und ``stufe2_pruefer`` (Schritte
2.5-2.12).

Schritt 2.1 (Betriebsart-Markierung) und 2.4 (Doppelprüfung Sperrkennzeichen) laufen
bereits in ``Versandengpass._pruefe`` selbst, bevor einer der beiden Prüfer hier
aufgerufen wird -- keine doppelte Prüfung nötig, siehe HED-42.

Was dieses Modul NICHT tut: den echten Versand an die Gegenstelle (B-6) und den
Übergang nach Z10 "versandt" -- der gehört demjenigen, der das Ergebnis des
tatsächlichen Sendeversuchs kennt (``Versandengpass._sende_an_gegenstelle``, heute
``NotImplementedError``). Besteht Stufe 2, bleibt der Vorschlag in Z8
"stufe2_laeuft"; erst eine künftige B-6-Anbindung darf nach Z10 oder zurück
überführen.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.marketdata.base import DataUnavailable
from app.marketdata.service import MarketService
from app.models import Asset, AssetSourceSymbol, Portfolio, Position, Proposal, Strategy
from app.models.proposals import (
    STATE_BOARD_FREIGEGEBEN,
    STATE_STUFE2_ABGELEHNT,
    STATE_STUFE2_LAEUFT,
)
from app.preisband import innerhalb_des_bandes, jenseits_des_stops
from app.risikolimits import LimitErgebnis, pruefe_alle_limits, tranchen_grenze
from app.trading.proposals import ist_frist_abgelaufen, transition
from app.trading.stufe1 import ProposalEingabe, sammle_kontext
from app.trading.versandengpass import Versandengpass, VersandOrder


async def _lade_proposal(session: AsyncSession, order: VersandOrder) -> Proposal | None:
    if order.proposal_id is None:
        return None
    return await session.get(Proposal, order.proposal_id)


async def pruefe_freigabe_nachweis(session: AsyncSession, order: VersandOrder) -> bool:
    """Schritt 2.2 (Freigabe-Nachweis vorhanden/gültig/nicht verwendet) + 2.3 (nicht
    verfallen). Bei Erfolg: Z7 "board_freigegeben" -> Z8 "stufe2_laeuft"."""
    proposal = await _lade_proposal(session, order)
    if proposal is None:
        return False
    if proposal.state != STATE_BOARD_FREIGEGEBEN:
        return False  # nicht freigegeben, bereits verwendet, oder anderer Zustand
    if proposal.approval_token is None:
        return False
    if ist_frist_abgelaufen(proposal):
        return False
    if order.quantity != proposal.size:
        return False  # Nachweis gehört nicht zu dieser Order (Größe passt nicht)

    await transition(
        session, proposal, STATE_STUFE2_LAEUFT, actor="system",
        reason="Freigabe-Nachweis gültig, Stufe 2 gestartet",
    )
    return True


async def _preis_lesbar_und_frisch(
    market: MarketService, asset: Asset
) -> tuple[bool, "object | None"]:
    try:
        view = await market.view(asset)
    except DataUnavailable:
        return False, None
    return view.fresh, view


async def _ablehnen(
    session: AsyncSession, proposal: Proposal, code: str, grund: str,
    ergebnisse: list[LimitErgebnis] | None = None,
) -> None:
    if ergebnisse is not None:
        proposal.stage2_limit_utilization = [e.to_json() for e in ergebnisse]
    await transition(
        session, proposal, STATE_STUFE2_ABGELEHNT, actor="system",
        reason=grund, rejection_reason_code=code,
    )


async def pruefe_stufe2(
    session: AsyncSession, market: MarketService, order: VersandOrder, *, tranche: str = "B1"
) -> bool:
    """Schritte 2.5-2.12. Jeder Fehlschlag: Ablehnung, kein stiller Ausgleich
    (Festlegung Abschnitt 6.2 "Keine Verkleinerung der Größe...")."""
    proposal = await _lade_proposal(session, order)
    if proposal is None:
        # Keine proposal-gebundene Order (Stop, Stornierung, Risikoabbau,
        # Strategie-Ausstieg, Not-Liquidation): das Preisband- und Limit-Nachprüfen
        # aus 6.2 gilt für board-freigegebene Einstiege/Erhöhungen, nicht für diese
        # Gattungen (Entwurf Abschnitt 6.2 Einleitung: "Nach Board-Freigabe"). Hier
        # bleibt die Mindestanforderung: ein lesbarer, frischer Kurs, bevor überhaupt
        # etwas das Haus verlässt. order.symbol ist das Börsensymbol (z. B.
        # "BTC/USDT"), nicht Asset.symbol ("BTC") -- Auflösung über die je Quelle
        # hinterlegte AssetSourceSymbol-Zuordnung.
        asset = await session.scalar(
            select(Asset)
            .join(AssetSourceSymbol, AssetSourceSymbol.asset_id == Asset.id)
            .where(
                AssetSourceSymbol.source == order.exchange,
                AssetSourceSymbol.symbol == order.symbol,
            )
        )
        if asset is None:
            return False
        fresh, _ = await _preis_lesbar_und_frisch(market, asset)
        return fresh

    if proposal.state != STATE_STUFE2_LAEUFT:
        return False

    strategy = await session.get(Strategy, proposal.strategy_id)
    if strategy is None or strategy.portfolio_id is None:
        await _ablehnen(
            session, proposal, "kein_verknuepftes_portfolio",
            "Strategie ohne Portfolio-Verknüpfung -- Stufe 2 nicht berechenbar",
        )
        return False
    portfolio = await session.get(Portfolio, strategy.portfolio_id)
    if portfolio is None:
        await _ablehnen(
            session, proposal, "portfolio_nicht_gefunden", "Verknüpftes Portfolio existiert nicht"
        )
        return False

    asset = await session.scalar(select(Asset).where(Asset.symbol == proposal.instrument))
    if asset is None:
        await _ablehnen(session, proposal, "asset_nicht_gefunden", "Instrument nicht im Katalog")
        return False

    # 2.5 / 2.6: Kurs lesbar und frisch (<= 30s für Krypto, app.marketdata.service
    # klassifiziert das bereits; Grenze für Nicht-Krypto liegt dort bei 20 Minuten).
    fresh, view = await _preis_lesbar_und_frisch(market, asset)
    if not fresh:
        await _ablehnen(
            session, proposal, "kursfrische",
            "Kurs nicht lesbar oder älter als die zulässige Grenze (Abschnitt 6.2)",
        )
        return False
    aktueller_kurs = view.quote.last

    # 2.9: aus einer Erhöhung darf keine Richtungsumkehr geworden sein.
    if proposal.proposal_kind == "increase":
        position = await session.scalar(
            select(Position).where(
                Position.portfolio_id == portfolio.id, Position.asset_id == asset.id
            )
        )
        bestehende_richtung = None
        if position is not None and position.quantity != 0:
            bestehende_richtung = "long" if position.quantity > 0 else "short"
        if bestehende_richtung != proposal.direction:
            await _ablehnen(
                session, proposal, "richtungsumkehr",
                "Position hat sich seit Freigabe so verändert, dass aus einer Erhöhung "
                "eine Richtungsumkehr würde (Schritt 2.9)",
            )
            return False

    # 2.10: aktueller Kurs innerhalb des Preisbands um den Vorschlagskurs.
    if not innerhalb_des_bandes(
        aktueller_kurs, proposal.proposal_price, proposal.stop_distance_pct
    ):
        await _ablehnen(
            session, proposal, "preisband",
            "Aktueller Kurs liegt außerhalb des freigegebenen Preisbands (Schritt 2.10)",
        )
        return False

    # 2.11: aktueller Kurs jenseits des Stops (von der Limit-Order unabhängige,
    # eigene Prüfung -- "die Limit-Order allein kann Bedingung (ii) nicht erzwingen").
    if not jenseits_des_stops(aktueller_kurs, proposal.stop_price, proposal.direction):
        await _ablehnen(
            session, proposal, "jenseits_stop",
            "Aktueller Kurs liegt nicht mehr jenseits des Stops (Schritt 2.11)",
        )
        return False

    # 2.12: Größe unverändert gegenüber dem freigegebenen Wert.
    if order.quantity != proposal.size:
        await _ablehnen(
            session, proposal, "groesse_veraendert",
            "Ordergröße weicht vom freigegebenen Wert ab (Schritt 2.12)",
        )
        return False

    # 2.7 / 2.8: alle Kennzahlen neu, gegen den aktuellen Stand -- derselbe Katalog
    # wie Stufe 1 (app.risikolimits.pruefe_alle_limits), neu gesammelter Kontext.
    eingabe = ProposalEingabe(
        instrument_asset_id=asset.id,
        direction=proposal.direction,
        size=proposal.size,
        proposal_price=proposal.proposal_price,
        stop_price=proposal.stop_price,
        stop_distance=proposal.stop_distance,
        stop_distance_pct=proposal.stop_distance_pct,
        proposal_kind=proposal.proposal_kind,
    )
    ctx = await sammle_kontext(
        session, market, portfolio, eingabe, tranche=tranche,
        tranche_eur=tranchen_grenze(tranche),
    )
    ergebnisse = pruefe_alle_limits(ctx)
    bruch = next((e for e in ergebnisse if e.gebrochen), None)
    if bruch is not None:
        await _ablehnen(session, proposal, bruch.limit, bruch.begruendung, ergebnisse)
        return False

    proposal.stage2_limit_utilization = [e.to_json() for e in ergebnisse]
    return True


def baue_versandengpass(market: MarketService, *, tranche: str = "B1") -> Versandengpass:
    """Ein korrekt verdrahteter Versand-Engpass für den Produktionspfad (HED-42 +
    HED-44). Ersetzt die fail-closed Standardwerte aus HED-42 durch die echten
    B-2-Prüfer. ``tranche`` kommt vom Aufrufer aus
    ``app.config.Settings.freigegebene_tranche``, nicht aus diesem Modul."""

    async def freigabe_nachweis(session: AsyncSession, order: VersandOrder) -> bool:
        return await pruefe_freigabe_nachweis(session, order)

    async def stufe2(session: AsyncSession, order: VersandOrder) -> bool:
        return await pruefe_stufe2(session, market, order, tranche=tranche)

    return Versandengpass(freigabe_nachweis_pruefer=freigabe_nachweis, stufe2_pruefer=stufe2)
