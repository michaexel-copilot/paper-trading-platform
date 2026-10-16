"""Exit-Fill-Erkennung: Stop- und ``auto_exit``-Schließungen in Position/Cash buchen
(HED-147, Entwurf Abschnitt 4.2, CRO-Abnahme HED-148/HED-150).

Nebenbefund aus HED-109 (B-12): die Proposal-Pipeline bucht seit HED-109 nur die
Einstiegsseite. Dieses Modul schließt die Exit-Seite -- ohne sie bleibt
``Position``/NAV nach jedem Stop- oder ``auto_exit``-Ausstieg falsch (offene
Position, obwohl real geschlossen), und jede davon abgeleitete S2-Kennzahl
(Realisierte Slippage, Kostendeckung, Max. Drawdown, Widerspruchsfreiheit
realisiertes Ergebnis) ist unbelastbar (CRO-Abnahme HED-145 Auflage 2).

Eigenes Modul statt Erweiterung von ``stop_pflicht.py``: dort sitzen
Fill-Erkennung und Stop-Platzierung bewusst in einem Durchlauf, weil sie eine
einzige Pflicht sind (Veto V2, B-12). Die Exit-Erkennung ist eine andere Pflicht
mit eigenem Intervall und darf einen fehlschlagenden Pflicht-Stop-Durchlauf nicht
mitreißen.

Scope-Grenzen (Entwurf Abschnitt 3, CRO-Abnahme HED-148, bewusst nicht gebaut):
- Nur Long-Exits (schließender ``side == "sell"``) -- Short-Covering ist über diese
  Pipeline strukturell nicht erreichbar (``versand_treiber`` sendet ausschließlich
  ``side == "buy"``-Einstiege; eine schließende Order in ``"buy"``-Richtung wäre ein
  Eindecken eines Shorts). Tritt der Fall trotzdem auf (z. B. künftige Erweiterung),
  wird fail closed nicht gebucht, nicht spekuliert.
- Nur OKX-Demo-Lesevorgänge -- ``Versandengpass.lese_order_status`` ist für jede
  Betriebsart außer PAPIER bereits fail-closed (liefert ``None``).
- Keine Nachbuchung historischer, nie gebuchter Exits (Festlegung N-6) -- Zeilen
  ohne Gegenstellen-Order-Id werden übersprungen, nicht rekonstruiert.
- Kein Verringern der Stop-Soll-Menge bei Teil-Ausstieg (bestehende Scope-Grenze
  aus B-12 R-5/E-12).
"""

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.marketdata.service import MarketService
from app.models.auto_exit import AutoExitLogEntry
from app.models.stop_order import StopOrderBuch
from app.trading.versandengpass import Versandengpass

log = logging.getLogger(__name__)

_OFFENE_STOP_ZUSTAENDE = ("offen", "nachfuehrung_laeuft")


async def pruefe_exit_fills_einmal(
    session_factory,
    engpass: Versandengpass,
    market: MarketService,
    *,
    exchange: str = "okx",
) -> int:
    """Eine Durchführung (Entwurf Abschnitt 4.2). Gibt die Anzahl der in diesem
    Durchlauf erfolgreich gebuchten Zeilen zurück (beide Teilaufgaben zusammen).

    Eigene Session je Zeile -- gleiche Begründung wie in
    ``app.trading.stop_pflicht.pruefe_fills_einmal``.

    Teilaufgabe A (Stop-Order) läuft vor Teilaufgabe B (``auto_exit``), Festlegung
    N-5: dient nur der Reproduzierbarkeit in Tests, das Ergebnis ist unabhängig von
    der Reihenfolge, weil beide gegen die dann aktuelle Position prüfen.
    """
    bearbeitet = await _pruefe_stop_fills(session_factory, engpass, market, exchange)
    bearbeitet += await _pruefe_auto_exit_fills(session_factory, engpass, market, exchange)
    return bearbeitet


# --- Teilaufgabe A: StopOrderBuch-Zeilen --------------------------------------------


async def _pruefe_stop_fills(
    session_factory, engpass: Versandengpass, market: MarketService, exchange: str
) -> int:
    async with session_factory() as lese_session:
        stop_ids = list(
            await lese_session.scalars(
                select(StopOrderBuch.id).where(
                    StopOrderBuch.zustand.in_(_OFFENE_STOP_ZUSTAENDE),
                    StopOrderBuch.gegenstellen_algo_id.is_not(None),
                )
            )
        )

    bearbeitet = 0
    for stop_id in stop_ids:
        try:
            async with session_factory() as session:
                stop = await session.get(StopOrderBuch, stop_id)
                if stop is None or stop.zustand not in _OFFENE_STOP_ZUSTAENDE:
                    continue
                if await _bearbeite_stop(session, stop, market, engpass, exchange):
                    bearbeitet += 1
        except Exception:
            log.exception("Exit-Fill-Erkennung (Stop): Stop %s fehlgeschlagen", stop_id)
    return bearbeitet


async def _bearbeite_stop(
    session: AsyncSession,
    stop: StopOrderBuch,
    market: MarketService,
    engpass: Versandengpass,
    exchange: str,
) -> bool:
    status = await engpass.lese_order_status(
        exchange, stop.gegenstellen_algo_id, stop.symbol, ist_algo_order=True
    )
    if status is None:
        # Entwurf Abschnitt 4.2 Teilaufgabe A, Schritt 3: nicht lesbar -- kein
        # stiller Zwischenzustand, kein Zustandswechsel, nächster Durchlauf.
        return False

    bisherige_menge = stop.gefuellte_menge_gebucht or Decimal(0)
    delta = status.gefuellte_menge - bisherige_menge
    gebucht = False
    if delta > 0:
        strategy_id, asset_id = await _resolve_stop_strategy_and_asset(session, stop, exchange)
        gebucht, _ = await _buche_exit_fill(
            session,
            market,
            strategy_id=strategy_id,
            asset_id=asset_id,
            side=stop.side,
            delta_menge=delta,
            durchschnittspreis=status.durchschnittspreis,
            kontext=f"Stop {stop.id}",
        )
        if gebucht:
            # Festlegung N-14: erst NACH erfolgreicher Buchung fortschreiben.
            stop.gefuellte_menge_gebucht = status.gefuellte_menge
        else:
            # Buchung fehlgeschlagen (N-2/N-10) -- Zeile bleibt vollständig
            # unverändert, nächster Durchlauf versucht es erneut.
            await session.commit()
            return False

    _fortschreibe_stop_zustand(stop, status.status, status.gefuellte_menge)
    await session.commit()
    return gebucht


def _fortschreibe_stop_zustand(stop: StopOrderBuch, status: str, gefuellte_menge: Decimal) -> None:
    """Entwurf Abschnitt 4.5, Tabelle -- nur erreicht, nachdem ein etwaiges Delta
    bereits erfolgreich gebucht wurde (Festlegung N-14, siehe ``_bearbeite_stop``)."""
    if status in ("gefuellt", "beendet") and gefuellte_menge > 0:
        if stop.zustand != "ausgeloest":
            stop.zustand = "ausgeloest"
            stop.beendet_at = utcnow()
    elif status in ("beendet", "abgelehnt") and gefuellte_menge <= 0:
        if stop.zustand != "storniert":
            stop.zustand = "storniert"
            stop.beendet_at = utcnow()
    # "teilgefuellt"/"offen": Zustand bleibt unverändert (Tabelle 4.5).


async def _resolve_stop_strategy_and_asset(
    session: AsyncSession, stop: StopOrderBuch, exchange: str
) -> tuple[int | None, int | None]:
    """Entwurf Abschnitt 4.2 Teilaufgabe A, Schritt 5: Strategie über
    ``stop.strategie_schluessel`` (enthält die ``Strategy.id``), Asset rückwärts
    über ``AssetSourceSymbol`` von ``stop.symbol``/``stop.exchange`` (``stop.symbol``
    ist das Börsensymbol, nicht ``Asset.symbol``)."""
    from app.models import AssetSourceSymbol

    try:
        strategy_id = int(stop.strategie_schluessel)
    except (TypeError, ValueError):
        return None, None
    asset_id = await session.scalar(
        select(AssetSourceSymbol.asset_id).where(
            AssetSourceSymbol.source == exchange, AssetSourceSymbol.symbol == stop.symbol
        )
    )
    return strategy_id, asset_id


# --- Teilaufgabe B: AutoExitLogEntry-Zeilen -----------------------------------------


async def _pruefe_auto_exit_fills(
    session_factory, engpass: Versandengpass, market: MarketService, exchange: str
) -> int:
    async with session_factory() as lese_session:
        eintrag_ids = list(
            await lese_session.scalars(
                select(AutoExitLogEntry.id).where(
                    AutoExitLogEntry.result == "versandt",
                    AutoExitLogEntry.gegenstellen_order_id.is_not(None),
                )
            )
        )

    bearbeitet = 0
    for eintrag_id in eintrag_ids:
        try:
            async with session_factory() as session:
                eintrag = await session.get(AutoExitLogEntry, eintrag_id)
                if eintrag is None or eintrag.result != "versandt":
                    continue
                if await _bearbeite_auto_exit(session, eintrag, market, engpass):
                    bearbeitet += 1
        except Exception:
            log.exception(
                "Exit-Fill-Erkennung (auto_exit): Eintrag %s fehlgeschlagen", eintrag_id
            )
    return bearbeitet


_ORDER_RICHTUNG_ZU_SEITE = {"verkauf": "sell", "kauf": "buy"}


async def _bearbeite_auto_exit(
    session: AsyncSession,
    eintrag: AutoExitLogEntry,
    market: MarketService,
    engpass: Versandengpass,
) -> bool:
    # Entwurf Abschnitt 4.2 Teilaufgabe B, Schritt 2: normale Order, keine Algo-Order
    # -- die Lücke aus Abschnitt 4.4 (Weg 3) betrifft diesen Zweig nicht
    # (Nachtrag-Handprobe HED-147 Abschnitt 2 Punkt 4, bestätigt mit ``ist_algo_order``
    # Standardwert ``False``).
    status = await engpass.lese_order_status(
        eintrag.exchange, eintrag.gegenstellen_order_id, eintrag.boersensymbol
    )
    if status is None:
        return False

    bisherige_menge = eintrag.gefuellte_menge_gebucht or Decimal(0)
    delta = status.gefuellte_menge - bisherige_menge
    gebucht = False
    neue_position: Decimal | None = None
    if delta > 0:
        side = _ORDER_RICHTUNG_ZU_SEITE.get(eintrag.order_direction)
        from app.models import Asset

        asset_id = await session.scalar(
            select(Asset.id).where(Asset.symbol == eintrag.instrument)
        )
        gebucht, neue_position = await _buche_exit_fill(
            session,
            market,
            strategy_id=eintrag.strategy_id,
            asset_id=asset_id,
            side=side,
            delta_menge=delta,
            durchschnittspreis=status.durchschnittspreis,
            kontext=f"auto_exit-Eintrag {eintrag.id}",
        )
        if gebucht:
            if eintrag.filled_at is None:
                eintrag.filled_at = utcnow()
            eintrag.gefuellte_menge_gebucht = status.gefuellte_menge
        else:
            await session.commit()
            return False

    _fortschreibe_auto_exit_ergebnis(eintrag, status.status, status.gefuellte_menge, neue_position)
    await session.commit()
    return gebucht


def _fortschreibe_auto_exit_ergebnis(
    eintrag: AutoExitLogEntry, status: str, gefuellte_menge: Decimal, neue_position: Decimal | None
) -> None:
    """Entwurf Abschnitt 4.5, Tabelle -- nur erreicht, nachdem ein etwaiges Delta
    bereits erfolgreich gebucht wurde (Festlegung N-14)."""
    if status == "gefuellt":
        eintrag.result = "gefuellt"
        if neue_position is not None:
            # Festlegung N-13: ersetzt den beim Versand eingetragenen
            # Erwartungswert durch die tatsächliche Position nach der Buchung.
            eintrag.position_after = neue_position
    elif status == "beendet" and gefuellte_menge > 0:
        eintrag.result = "teilgefuellt"
        if neue_position is not None:
            eintrag.position_after = neue_position
    elif status == "beendet" and gefuellte_menge <= 0:
        # Von der Tabelle in Entwurf 4.5 nicht ausdrücklich benannter Randfall
        # (stornierte/abgelaufene Order ohne jede Füllung) -- dieselbe Behandlung
        # wie "abgelehnt": keine Füllung fand statt, keine Buchung nötig. Konsistent
        # mit der analogen Zeile der Stop-Order-Tabelle ("beendet" + Menge 0 ->
        # storniert).
        eintrag.result = "von_gegenstelle_abgelehnt"
    elif status == "abgelehnt":
        eintrag.result = "von_gegenstelle_abgelehnt"
    # "teilgefuellt"/"offen": result bleibt "versandt" (Tabelle 4.5), filled_at/
    # gefuellte_menge_gebucht sind oben bereits beim ersten Fill fortgeschrieben.


# --- Gemeinsame Buchung (Festlegung N-1/N-2, Entwurf Abschnitt 4.1) -----------------


async def _buche_exit_fill(
    session: AsyncSession,
    market: MarketService,
    *,
    strategy_id: int | None,
    asset_id: int | None,
    side: str | None,
    delta_menge: Decimal,
    durchschnittspreis: Decimal | None,
    kontext: str,
) -> tuple[bool, Decimal | None]:
    """Gemeinsame Vorprüfung (Festlegung N-2) und Buchung für Exit-Fills (Teilaufgabe
    A und B). Gibt ``(erfolgreich, neue_position_quantity)`` zurück --
    ``neue_position_quantity`` nur bei Erfolg gesetzt.

    Fail closed, in dieser Reihenfolge: kein Fill-Preis (N-10) -> keine schließende
    Seite (Abschnitt 3 Punkt 1, Short-Covering nicht gebaut) -> Strategie ohne
    Portfolio -> Portfolio existiert nicht -> Asset nicht auflösbar -> keine
    Position-Zeile oder Zeile kleiner als das gemeldete Delta (N-2). Jede Stufe
    protokolliert und bucht nichts.
    """
    from app.models import Asset, Portfolio, Position, Strategy
    from app.trading.engine import amounts
    from app.trading.fill_buchung import buche_fill
    from app.trading.orders import pricing_for

    if durchschnittspreis is None:
        log.error(
            "Exit-Fill-Buchung (%s): kein positiver Fill-Preis von der Gegenstelle -- "
            "nicht gebucht (Festlegung N-10).",
            kontext,
        )
        return False, None
    if side != "sell":
        # Abschnitt 3 Punkt 1: Short-Covering ("buy" schließt eine Short-Position)
        # ist über diese Pipeline heute nicht erreichbar -- dokumentierte Grenze,
        # kein stiller Fallback, keine Buchung.
        log.error(
            "Exit-Fill-Buchung (%s): schließende Seite %r ist kein Verkauf -- "
            "Short-Covering ist über diese Pipeline nicht gebaut (Entwurf Abschnitt "
            "3 Punkt 1), nicht gebucht.",
            kontext,
            side,
        )
        return False, None
    if strategy_id is None or asset_id is None:
        log.error(
            "Exit-Fill-Buchung (%s): Strategie oder Asset nicht auflösbar -- nicht "
            "gebucht (Festlegung N-2).",
            kontext,
        )
        return False, None

    strategy = await session.get(Strategy, strategy_id)
    if strategy is None or strategy.portfolio_id is None:
        log.error(
            "Exit-Fill-Buchung (%s): Strategie %s ohne aufgelöstes Portfolio -- "
            "nicht gebucht (Festlegung N-2).",
            kontext,
            strategy_id,
        )
        return False, None
    portfolio = await session.scalar(
        select(Portfolio).where(Portfolio.id == strategy.portfolio_id).with_for_update()
    )
    if portfolio is None:
        log.error(
            "Exit-Fill-Buchung (%s): Portfolio %s existiert nicht -- nicht gebucht "
            "(Festlegung N-2).",
            kontext,
            strategy.portfolio_id,
        )
        return False, None
    asset = await session.get(Asset, asset_id)
    if asset is None:
        log.error(
            "Exit-Fill-Buchung (%s): Asset %s existiert nicht -- nicht gebucht "
            "(Festlegung N-2).",
            kontext,
            asset_id,
        )
        return False, None

    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == asset_id)
    )
    if position is None or position.quantity < delta_menge:
        # Keine Zeile oder eine zu kleine Zeile ist ein Widerspruch zwischen
        # eigenem Buch und Gegenstelle, kein Rundungsfall -- Festlegung N-2: kein
        # Teilbuchen, nichts geglättet, sichtbar als Fehler, nicht als Vermutung.
        log.error(
            "Exit-Fill-Buchung (%s): Position fehlt oder ist kleiner als das "
            "gemeldete Delta %s -- nicht gebucht (Festlegung N-2, Widerspruch "
            "Buch/Gegenstelle).",
            kontext,
            delta_menge,
        )
        return False, None

    pricing = await pricing_for(session, market, portfolio, asset, view=None)
    result = amounts("sell", durchschnittspreis, delta_menge, "taker", pricing)
    await buche_fill(
        session,
        portfolio,
        asset_id,
        "sell",
        durchschnittspreis,
        delta_menge,
        pricing.fx_rate,
        result,
        zeile_behalten=True,
    )
    neue_position = await session.scalar(
        select(Position.quantity).where(
            Position.portfolio_id == portfolio.id, Position.asset_id == asset_id
        )
    )
    return True, (neue_position if neue_position is not None else Decimal(0))
