"""Versand-Treiber: Z8 "stufe2_laeuft" -> Z10 "versandt" (B-12, HED-84, Entwurf
Abschnitt 5).

Schließt die von HED-36 Abschnitt 12.9 und ``app.trading.stufe2`` (Zeile 11-16: "erst
eine künftige B-6-Anbindung darf nach Z10 oder zurück überführen") benannte, bisher
fehlende Strecke zwischen Board-Freigabe und tatsächlichem Versand. Ohne diesen
Treiber bleibt jeder board-freigegebene Vorschlag für immer in Z7 "board_freigegeben"
liegen -- es gibt sonst keinen Baustein, der ``Versandengpass.send`` für einen
Einstieg aufruft; ``app.trading.dashboard.approve`` setzt nur den Zustand, ohne den
Versand-Engpass je anzufassen.

Aufgebaut wie ``app.trading.auto_exit`` (Vorbild Engpass-Aufrufer): eine testbare
Funktion (``treibe_einmal``), die Endlos-Schleife lebt in ``app.main``. Läuft nur im
Prozess, der auch den Matcher hält (Festlegung F-3) -- derselbe Grund wie bei
``_notify_new_proposals``: kein zweiter Prozess derselben Installation darf denselben
Vorschlag parallel versenden.
"""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.marketdata.service import MarketService
from app.models import Asset, AssetSourceSymbol, Proposal
from app.models.proposals import STATE_BOARD_FREIGEGEBEN, STATE_STUFE2_ABGELEHNT, STATE_VERSANDT
from app.risikolimits import LimitErgebnis
from app.trading.proposals import transition
from app.trading.stufe2 import berechne_limitauslastung_snapshot
from app.trading.versandengpass import Versandengpass, VersandOrder

log = logging.getLogger(__name__)

LimitauslastungPruefer = Callable[[AsyncSession, MarketService, Proposal], Awaitable[list | None]]

# Pfad 18, Entwurf 9.6, CRO-Entscheidung HED-191 Abschnitt 3b: Platzhalter, wenn der
# Snapshot nicht berechenbar ist (kein Markt übergeben, Hook fehlgeschlagen) -- hält
# ``stage2_limit_utilization`` in der von Stufe 1/2 gewohnten Listenform, statt einer
# rohen Zeichenkette.
_LIMITAUSLASTUNG_NICHT_BERECHENBAR = LimitErgebnis(
    limit="pfad18_sperre",
    berechenbar=False,
    gebrochen=True,
    auslastung_pct=None,
    begruendung="Limitauslastung-Snapshot bei Sperren-Ablehnung nicht berechenbar (Pfad 18)",
).to_json()

# Korrektur gegenüber Entwurf HED-84 Fassung 1.0, Abschnitt 5.2, Schritt 1 (dort:
# "Alle Vorschläge mit state == STATE_STUFE2_LAEUFT laden"): gegen den tatsächlichen
# Code geprüft (``app.trading.stufe2.pruefe_freigabe_nachweis``,
# ``app.trading.dashboard.approve``) zeigt sich, dass der Z7->Z8-Übergang
# ausschließlich innerhalb von ``pruefe_freigabe_nachweis`` passiert, und zwar nur,
# wenn ``proposal.state == STATE_BOARD_FREIGEGEBEN`` beim Aufruf gilt
# (``app/trading/stufe2.py`` Zeile 49). ``approve()`` selbst setzt nur Z7, ruft aber
# nie ``Versandengpass.send`` -- vor diesem Treiber existiert in der Installation kein
# Baustein, der einen Z7-Vorschlag jemals anfasst. Eine Abfrage auf
# ``STATE_STUFE2_LAEUFT`` würde deshalb nie etwas finden: jeder frisch freigegebene
# Vorschlag bliebe für immer in Z7 liegen. Der Treiber fragt daher Z7 ab; der
# Z8-Zwischenzustand entsteht als Seiteneffekt des ersten ``send()``-Aufrufs selbst
# (siehe ``_treibe_proposal``), nicht als eigener Abfragefilter. Dem CRO zur Kenntnis
# gegeben (Statusmeldung auf HED-84) -- Mechanismus und Zielzustand sind unverändert,
# nur der Abfragefilter ist korrigiert.

_GATTUNG_JE_ART = {"entry": "einstieg", "increase": "erhoehung"}


async def _boersensymbol(session: AsyncSession, instrument: str, exchange: str) -> str | None:
    asset = await session.scalar(select(Asset).where(Asset.symbol == instrument))
    if asset is None:
        return None
    return await session.scalar(
        select(AssetSourceSymbol.symbol).where(
            AssetSourceSymbol.asset_id == asset.id, AssetSourceSymbol.source == exchange
        )
    )


def _quote_waehrung(symbol: str) -> str | None:
    """Harter Umfang (Entwurf Abschnitt 2.1): nur USDC-Paare. Börsensymbol-Konvention
    "BASIS/QUOTE", wie überall in dieser Installation (``AssetSourceSymbol.symbol``)."""
    teile = symbol.split("/")
    return teile[1] if len(teile) == 2 else None


async def treibe_einmal(
    session_factory,
    engpass: Versandengpass,
    *,
    exchange: str = "okx",
    market: MarketService | None = None,
    tranche: str = "B1",
    limitauslastung_pruefer: LimitauslastungPruefer = berechne_limitauslastung_snapshot,
) -> int:
    """Eine Durchführung (Entwurf Abschnitt 5.2, Abfragefilter korrigiert -- siehe
    Moduldokumentation oben). Gibt die Anzahl tatsächlich versandter Vorschläge
    zurück (für Tests und Protokollierung).

    Eine eigene Session je Vorschlag (nicht eine gemeinsame Session für den ganzen
    Durchlauf): scheitert ``engpass.send`` mit einer nicht aufgefangenen Ausnahme aus
    der Gegenstelle (Timeout, siehe ``app.trading.papier.okx_demo``), bliebe in einer
    gemeinsamen Session sonst ein bereits mutiertes, aber nie committetes
    Proposal-Objekt hängen -- der nächste erfolgreiche Commit im selben Durchlauf
    würde diese Mutation ungewollt mit festschreiben. F-5 ("keine stille
    Wiederholung in derselben Runde") gilt je Vorschlag, nicht für den ganzen Batch.

    ``market``/``tranche`` (Pfad 18, HED-191 Abschnitt 3b): nur für den
    dokumentarischen Limitauslastung-Snapshot bei einer Sperren-Ablehnung gebraucht --
    ``market=None`` (z. B. in bestehenden Tests ohne Marktanbindung) heißt lediglich
    "nicht berechenbar", blockiert aber nichts.
    """
    async with session_factory() as lese_session:
        proposal_ids = list(
            await lese_session.scalars(
                select(Proposal.id)
                .where(Proposal.state == STATE_BOARD_FREIGEGEBEN)
                .order_by(Proposal.id.asc())
            )
        )

    versandt = 0
    for proposal_id in proposal_ids:
        try:
            async with session_factory() as session:
                proposal = await session.get(Proposal, proposal_id)
                if proposal is None or proposal.state != STATE_BOARD_FREIGEGEBEN:
                    # Zwischenzeitlich von einem anderen Durchlauf bereits bearbeitet
                    # (sollte unter F-3 nicht vorkommen, fail closed trotzdem geprüft).
                    continue
                if await _treibe_proposal(
                    session, proposal, exchange, engpass,
                    market=market, tranche=tranche,
                    limitauslastung_pruefer=limitauslastung_pruefer,
                ):
                    versandt += 1
        except Exception:
            log.exception("Versand-Treiber: Vorschlag %s fehlgeschlagen", proposal_id)
    return versandt


async def _lehne_wegen_sperre_ab(
    session: AsyncSession,
    proposal: Proposal,
    market: MarketService | None,
    tranche: str,
    limitauslastung_pruefer: LimitauslastungPruefer,
) -> None:
    """Pfad 18 (Entwurf 9.6, CRO-Entscheidung HED-191 Abschnitt 3): ein während
    aktiver Sperre wiederholt abgelehnter Freigabe-Versuch bekommt einen echten
    Z7->Z9-Übergang mit eigenem ``rejection_reason_code`` -- statt für immer in Z7
    liegenzubleiben (wie vor HED-192/HED-185 jeder weitere Durchlauf nur eine weitere
    ``VersandEngpassEintrag``-Zeile mit ``unterdrueckt_durch_sperre`` erzeugt hat,
    ohne je den Zustand des Vorschlags selbst zu ändern).

    B-2s Fail-closed-Grundsatz ("Kennzahl nicht berechenbar = kein Handel") gilt hier
    bewusst nicht streng (3b): die Order ist durch die Sperre bereits verhindert,
    diese Zeile ist rein dokumentarisch -- ein fehlgeschlagener Snapshot blockiert
    den Übergang nicht, sondern wird selbst als "nicht berechenbar" protokolliert.
    """
    snapshot = None
    if market is not None:
        try:
            snapshot = await limitauslastung_pruefer(session, market, proposal, tranche=tranche)
        except Exception:
            log.exception(
                "Versand-Treiber: Limitauslastung-Snapshot für Vorschlag %s bei "
                "Sperren-Ablehnung fehlgeschlagen (Pfad 18) -- als nicht berechenbar "
                "protokolliert.",
                proposal.id,
            )
            snapshot = None
    proposal.stage2_limit_utilization = (
        snapshot if snapshot is not None else [_LIMITAUSLASTUNG_NICHT_BERECHENBAR]
    )
    await transition(
        session, proposal, STATE_STUFE2_ABGELEHNT, actor="system",
        reason="Versand-Treiber: freigegebener Vorschlag während aktiver Sperre "
        "abgelehnt (Pfad 18, B-3/B-15)",
        rejection_reason_code="stufe2_abgelehnt_wegen_sperre",
    )
    await session.commit()


async def _treibe_proposal(
    session: AsyncSession,
    proposal: Proposal,
    exchange: str,
    engpass: Versandengpass,
    *,
    market: MarketService | None = None,
    tranche: str = "B1",
    limitauslastung_pruefer: LimitauslastungPruefer = berechne_limitauslastung_snapshot,
) -> bool:
    # Ausschlüsse vor jedem Gegenstellen-Kontakt (Entwurf Abschnitt 5.2, fail closed,
    # keine Order -- der Vorschlag bleibt in Z8 und wird vom Fristwächter abgeräumt,
    # falls er nie versandfähig wird).
    if proposal.mode != "paper":
        return False
    if proposal.direction == "short":
        log.error(
            "Versand-Treiber: Vorschlag %s mit direction='short' -- Spot kennt keinen "
            "Short (F-4), kein Versand.",
            proposal.id,
        )
        return False
    if proposal.limit_price is None:
        log.error(
            "Versand-Treiber: Vorschlag %s ohne eingefrorenen Grenzpreis -- kein "
            "zulässiger Versand.",
            proposal.id,
        )
        return False

    symbol = await _boersensymbol(session, proposal.instrument, exchange)
    if symbol is None:
        log.error(
            "Versand-Treiber: kein Börsensymbol für %s/%s -- kein Versand.",
            proposal.instrument,
            exchange,
        )
        return False
    if _quote_waehrung(symbol) != "USDC":
        log.error(
            "Versand-Treiber: Börsensymbol %s ist kein USDC-Paar -- harter Umfang "
            "(Entwurf Abschnitt 2.1), kein Versand.",
            symbol,
        )
        return False

    order = VersandOrder(
        gattung=_GATTUNG_JE_ART.get(proposal.proposal_kind, "einstieg"),
        strategie_schluessel=str(proposal.strategy_id),
        exchange=exchange,
        symbol=symbol,
        side="buy" if proposal.direction == "long" else "sell",
        quantity=proposal.size,
        order_type="limit",
        proposal_id=proposal.id,
        limit_price=proposal.limit_price,
    )
    ergebnis = await engpass.send(session, order)
    if not ergebnis.angenommen:
        # Drei Fälle: (a) Sperrkennzeichen aktiv (Schritt 3-5, Pfad 18) -- eigener
        # Z7->Z9-Übergang, siehe ``_lehne_wegen_sperre_ab``, statt für immer in Z7
        # liegenzubleiben; (b) jede andere technische Ablehnung in Schritt 1-5 von
        # Versandengpass._pruefe (z. B. schluessel_nicht_verwendbar) -- der Freigabe-
        # Nachweis-Check (Schritt 6) wurde dabei nie erreicht, der Vorschlag bleibt in
        # Z7 und wird im nächsten Durchlauf erneut versucht; (c) der Freigabe-Nachweis
        # ist bestanden (Z7->Z8), aber pruefe_stufe2 hat inhaltlich abgelehnt und den
        # Vorschlag dabei bereits selbst nach Z9 "stufe2_abgelehnt" überführt.
        if ergebnis.ablehnungsgrund == "unterdrueckt_durch_sperre":
            await _lehne_wegen_sperre_ab(
                session, proposal, market, tranche, limitauslastung_pruefer
            )
        return False

    proposal.sent_at = utcnow()
    proposal.gegenstellen_order_id = ergebnis.gegenstellen_order_id
    await transition(
        session, proposal, STATE_VERSANDT, actor="system",
        reason="Versand-Treiber: Einstiegs-Order angenommen (B-12)",
    )
    await session.commit()
    return True
