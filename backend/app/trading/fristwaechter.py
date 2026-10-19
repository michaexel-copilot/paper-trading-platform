"""Pfad 7 -- Fristwächter für die beiden Kill-Switch-Fristen aus HED-8 Fassung 1.4
§6.1/§6.3 (CRO-Entscheidung HED-191 Abschnitt 2, Option C/A -- Dokument `ergebnis`).

**Geltungsbereich, bewusst eng:** nur Eskalation und Protokollzeile, keine
automatische Order. Eine automatische Risikoabbau-Order ohne spezifizierte Größen-,
Reihenfolge- und Fehlerfallregel wäre selbst der teuerste Einzelfehler, den die
Firma kennt (HED-191). Der volle, handelnde Umfang bleibt Gate-B-Lücke, siehe
Folgeentwurf HED-196.

Zwei Fristen, beide ab dem Auslösezeitpunkt der jeweiligen Sperre, nicht ab deren
``updated_at`` (siehe :func:`ausloese_zeitpunkt` für die Begründung):

- **Stufe 1** (je Strategie, 24h, HED-8 §6.3 Punkt 1): die Strategie soll ihre
  offene Position "planmäßig schließen". Bleibt nach Ablauf noch eine offene
  Position -- oder ist das nicht entscheidbar, weil die Strategie keinem
  Portfolio zugeordnet ist (derselbe Lückenfall wie in
  ``app.trading.stufe1.lauf_stufe1``) -- wird eskaliert.
- **Stufe 2** (firmenweit, 4h, HED-8 §6.1): Brutto-Exposure soll auf <= 20% des
  aktuellen NAV zurückgeführt werden. NAV und Brutto-Exposure werden zum
  *Messzeitpunkt* (Fristablauf) frisch berechnet, nicht zum Auslösezeitpunkt
  eingefroren (HED-191, technische Festlegung). Firmenweit = Summe über alle
  Portfolios -- bis Gate C ist das ohnehin das gesamte Portfolio (Krypto).

**Bekannte Ungenauigkeit, wie im übrigen Buch dieser Installation:** "offene
Position der Strategie" wird über ``Strategy.portfolio_id`` aufgelöst (wie
``app.trading.stufe1.lauf_stufe1`` und ``app.trading.risikoauszug``) -- teilen sich
zwei Strategien ein Portfolio, kann die geprüfte Position einer anderen Strategie
gehören. Kein neuer Mangel dieses Moduls, sondern dieselbe Buchführungsgranularität
wie überall sonst in diesem Repo.

**Eskalation ist kein Dauerspam:** höchstens eine Eskalationszeile je
(Kennzeichen, Auslösezeitpunkt)-Paar (``FristEskalation``, Unique-Constraint). Eine
fortbestehende Verletzung erzeugt keine zweite Zeile, solange die Sperre
ununterbrochen gesetzt bleibt -- erst eine Aufhebung und ein neues Setzen eröffnen
einen neuen Auslösezeitpunkt und damit eine neue mögliche Eskalation. Wird die
Frist beim ersten Prüfdurchlauf nach Ablauf bestanden (z. B. Exposure zufällig schon
<= 20%), aber später während derselben ununterbrochenen Sperre erneut verletzt,
eskaliert dieser Durchlauf erneut -- die Frist verlangt dauerhafte Einhaltung ab
Fristablauf, nicht nur eine einmalige Momentaufnahme.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.marketdata.service import MarketService
from app.models import Portfolio, Position, Strategy
from app.models.fristwaechter import FristEskalation
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen
from app.trading.valuation import value_portfolio

log = logging.getLogger(__name__)

STUFE1_FRIST = timedelta(hours=24)
STUFE2_FRIST = timedelta(hours=4)
# Entwurf-Ergänzung HED-185 Abschnitt 2b: eigener Fristwächter analog
# app.trading.proposals.verfalle_ueberfaellige_vorschlaege, eigenes Intervall (gröber
# als dessen 30s -- die hier bewachten Fristen sind Stunden, nicht Sekunden).
FRISTWAECHTER_INTERVAL_SECONDS = 300


async def ausloese_zeitpunkt(
    session: AsyncSession, *, kennzeichen_typ: str, strategie_schluessel: str | None = None,
) -> datetime | None:
    """Frühester "gesetzt"-Zeitpunkt der laufenden, ununterbrochenen Sperre.

    Nicht ``updated_at`` auf ``FirmenweitesSperrkennzeichen``/``StrategieSperrkennzeichen``
    selbst -- das wird bei jedem weiteren Setzen während einer bereits laufenden
    Sperre überschrieben (z. B. ein zweiter, andersartiger automatischer Auslöser
    während bereits aktiver Stufe 2), was die Frist bei jedem Folge-Trigger
    zurücksetzen würde -- das Gegenteil von dem, was die Frist leisten soll.

    Läuft deshalb über das Append-only-Protokoll (``SperrkennzeichenProtokoll``,
    sortiert nach ``id`` -- monoton, robuster als ``occurred_at`` bei Gleichstand)
    und merkt sich den ersten "gesetzt"-Eintrag seit dem letzten "aufgehoben".
    ``None``, wenn das Kennzeichen laut Protokoll gerade nicht gesetzt ist.
    """
    zeilen = await session.scalars(
        select(SperrkennzeichenProtokoll)
        .where(
            SperrkennzeichenProtokoll.kennzeichen_typ == kennzeichen_typ,
            SperrkennzeichenProtokoll.strategie_schluessel == strategie_schluessel,
        )
        .order_by(SperrkennzeichenProtokoll.id)
    )
    kandidat: datetime | None = None
    for zeile in zeilen:
        if zeile.aktion == "gesetzt":
            if kandidat is None:
                kandidat = zeile.occurred_at
        else:  # "aufgehoben"
            kandidat = None
    return kandidat


async def _bereits_eskaliert(
    session: AsyncSession,
    *,
    kennzeichen_typ: str,
    strategie_schluessel: str | None,
    trigger: datetime,
) -> bool:
    vorhanden = await session.scalar(
        select(FristEskalation).where(
            FristEskalation.kennzeichen_typ == kennzeichen_typ,
            FristEskalation.strategie_schluessel == strategie_schluessel,
            FristEskalation.ausloese_zeitpunkt == trigger,
        )
    )
    return vorhanden is not None


async def _schreibe_eskalation(
    session: AsyncSession,
    *,
    kennzeichen_typ: str,
    strategie_schluessel: str | None,
    frist_typ: str,
    trigger: datetime,
    befund: str,
) -> None:
    session.add(
        FristEskalation(
            kennzeichen_typ=kennzeichen_typ,
            strategie_schluessel=strategie_schluessel,
            frist_typ=frist_typ,
            ausloese_zeitpunkt=trigger,
            befund=befund,
        )
    )
    log.warning(
        "Fristwächter-Eskalation: kennzeichen_typ=%s strategie_schluessel=%s frist_typ=%s -- %s",
        kennzeichen_typ, strategie_schluessel, frist_typ, befund,
    )


async def _strategie_hat_offene_position(
    session: AsyncSession, strategie_schluessel: str
) -> bool | None:
    """``None`` = nicht entscheidbar (kein gültiger Schlüssel, keine Strategie, kein
    verknüpftes Portfolio) -- derselbe Lückenfall wie in
    ``app.trading.stufe1.lauf_stufe1`` ("Strategie ohne Portfolio-Verknüpfung")."""
    try:
        strategy_id = int(strategie_schluessel)
    except (TypeError, ValueError):
        return None
    strategy = await session.get(Strategy, strategy_id)
    if strategy is None or strategy.portfolio_id is None:
        return None
    positionen = await session.scalars(
        select(Position).where(Position.portfolio_id == strategy.portfolio_id)
    )
    return any(p.quantity != 0 for p in positionen)


async def pruefe_stufe1_fristen(session_factory) -> list[str]:
    """Eine Durchführung (Stufe 1, 24h). Gibt die Schlüssel der Strategien zurück,
    für die in diesem Lauf eskaliert wurde (für Protokoll/Tests).

    Ein Fehler bei einer einzelnen Strategie darf die anderen nicht blockieren
    (gleiches Muster wie ``verfalle_ueberfaellige_vorschlaege``)."""
    now = utcnow()
    eskaliert: list[str] = []
    async with session_factory() as session:
        kennzeichen = list(
            await session.scalars(
                select(StrategieSperrkennzeichen).where(
                    StrategieSperrkennzeichen.gesetzt.is_(True)
                )
            )
        )
        for zeile in kennzeichen:
            schluessel = zeile.strategie_schluessel
            try:
                trigger = await ausloese_zeitpunkt(
                    session, kennzeichen_typ="strategie", strategie_schluessel=schluessel
                )
                if trigger is None or now - trigger < STUFE1_FRIST:
                    continue
                if await _bereits_eskaliert(
                    session, kennzeichen_typ="strategie",
                    strategie_schluessel=schluessel, trigger=trigger,
                ):
                    continue
                offen = await _strategie_hat_offene_position(session, schluessel)
                if offen is False:
                    continue  # Ziel erreicht -- keine Eskalation
                befund = (
                    "Strategie ohne verknüpftes Portfolio -- Stufe-1-Frist nicht "
                    "entscheidbar (fail closed, wie Stufe 1 bei Vorschlägen)."
                    if offen is None else
                    "Strategie hat nach Ablauf der 24h-Frist (Stufe 1, HED-8 §6.3 "
                    "Punkt 1) noch eine offene Position."
                )
                await _schreibe_eskalation(
                    session, kennzeichen_typ="strategie", strategie_schluessel=schluessel,
                    frist_typ="stufe1_schliessung", trigger=trigger, befund=befund,
                )
                eskaliert.append(schluessel)
            except Exception:
                log.exception(
                    "Stufe-1-Fristwächter: Prüfung für Strategie %s fehlgeschlagen", schluessel
                )
        await session.commit()
    return eskaliert


async def _firmenweite_exposure_befund(session: AsyncSession, market: MarketService) -> str | None:
    """``None`` = Frist bestanden (Exposure <= 20% NAV). Sonst der Eskalationstext.

    NAV/Brutto-Exposure zum *Messzeitpunkt* (Aufruf dieser Funktion), nicht zum
    Auslösezeitpunkt eingefroren (HED-191, technische Festlegung zu 2c). Nicht
    berechenbar (keine Portfolios, NAV <= 0) zählt als Bruch -- fail closed wie
    überall in dieser Politik, keine Sonderregel (HED-191)."""
    portfolios = list(await session.scalars(select(Portfolio)))
    if not portfolios:
        return "Kein Portfolio vorhanden -- NAV firmenweit nicht berechenbar, fail closed."

    nav = Decimal(0)
    brutto_exposure = Decimal(0)
    for portfolio in portfolios:
        valuation = await value_portfolio(session, market, portfolio)
        nav += valuation.total_value
        brutto_exposure += sum((p.market_value for p in valuation.positions), start=Decimal(0))

    if nav <= 0:
        return f"NAV firmenweit {nav} <= 0 -- nicht berechenbar, fail closed."

    anteil = brutto_exposure / nav * Decimal(100)
    if anteil > Decimal(20):
        return (
            f"Brutto-Exposure firmenweit {anteil:.2f}% NAV > 20% nach Ablauf der 4h-Frist "
            f"(HED-8 §6.1) -- NAV={nav}, Brutto-Exposure={brutto_exposure}."
        )
    return None


async def pruefe_stufe2_fristen(session_factory, market: MarketService) -> bool:
    """Eine Durchführung (Stufe 2, 4h). Gibt zurück, ob in diesem Lauf eskaliert
    wurde (für Protokoll/Tests)."""
    now = utcnow()
    eskaliert = False
    async with session_factory() as session:
        try:
            zeile = await session.scalar(select(FirmenweitesSperrkennzeichen).limit(1))
            if zeile is not None and zeile.gesetzt:
                trigger = await ausloese_zeitpunkt(session, kennzeichen_typ="firmenweit")
                if trigger is not None and now - trigger >= STUFE2_FRIST and not (
                    await _bereits_eskaliert(
                        session, kennzeichen_typ="firmenweit",
                        strategie_schluessel=None, trigger=trigger,
                    )
                ):
                    try:
                        befund = await _firmenweite_exposure_befund(session, market)
                    except Exception:
                        log.exception("Stufe-2-Fristwächter: Exposure-Berechnung fehlgeschlagen")
                        befund = (
                            "NAV/Brutto-Exposure zum Messzeitpunkt nicht berechenbar "
                            "(unerwarteter Fehler bei der Bewertung) -- fail closed."
                        )
                    if befund is not None:
                        await _schreibe_eskalation(
                            session, kennzeichen_typ="firmenweit", strategie_schluessel=None,
                            frist_typ="stufe2_exposure_rueckfuehrung", trigger=trigger,
                            befund=befund,
                        )
                        eskaliert = True
        except Exception:
            log.exception("Stufe-2-Fristwächter fehlgeschlagen")
        await session.commit()
    return eskaliert
