"""Automatischer strategiegetriebener Ausstieg (Entwurf HED-36 Fassung 1.1 Abschnitt 10,
CRO-freigegeben; Umsetzung HED-48).

Geltungsbereich eng gefasst (10.1): ausschließlich das vollständige oder teilweise
Schließen einer bereits bestehenden, freigegebenen Position in bestehender Richtung,
nie mehr. Jede Positionserhöhung, Richtungsänderung oder Order über die offene Größe
hinaus läuft über den normalen Einzelfreigabe-Weg
(``app.trading.proposals.create_proposal``), nicht über dieses Modul.

Reicht an den bereits CRO-freigegebenen Versand-Engpass (HED-42) weiter und lässt dort
dieselbe Stufe-2-Nachprüfung laufen wie jede andere Order (HED-44,
``app.trading.stufe2.pruefe_stufe2``) -- "gleiche Grenze, gleiche Messweise, gleiche
Quelle" für die Kursfrische (Entwurf 10.3), keine eigene, zweite Implementierung. Für
eine Order ohne Proposal (``proposal_id=None``, Gattung "strategie_ausstieg") prüft
``pruefe_stufe2`` genau das: lesbarer, frischer Kurs -- nichts mehr, nichts weniger
(siehe dort, Zweig "Keine proposal-gebundene Order").

Branch-Hinweis für die CRO-Abnahme: dieser Branch baut auf hed-44-b2-stufe1-stufe2 +
hed-47-kill-switch-zuschnitt auf, nicht auf main. Gebraucht werden
``Strategy.portfolio_id`` (HED-44, für die offene Position) und die
Sperrkennzeichen-Prüfung in ``create_proposal`` (HED-47, für Teil 2 "Gegenposition",
Entwurf 10.4 Festlegung 4). Beide Elternbranches sind CRO-abgenommen (HED-61, HED-59),
aber noch nicht in main gemergt -- siehe PR-Beschreibung für Einzelheiten und das
Rebase-/Retarget-Vorgehen, sobald sie dort ankommen.

Reversal-Erkennung (Entwurf 10.4): ``AusstiegsSignal`` lässt die Strategie das Teil-2-
"Gegenposition"-Vorhaben explizit über ``gegenposition_size`` erklären, statt es aus
Zahlen zurückzurechnen. Grund: ob eine Größe über der offenen Position daher kommt,
dass sich die Position seit dem Signal verkleinert hat (Entwurf 10.2, reine Begrenzung,
Festlegung A4 Fall 1) oder daher, dass die Strategie wirklich eine Gegenposition will
(Fall 2, Reversal), ist aus der Zahl allein nicht entscheidbar -- nur die Strategie
kennt die Ursache. Ist ``gegenposition_size`` nicht gesetzt, wird jede Überschreitung
immer als Fall 1 behandelt (begrenzen, protokollieren, kein Teil 2).

Known gap, nicht in diesem Umfang gebaut: A1 (offene Position lesbar) liest nur das
eigene Buch (``app.models.portfolios.Position``). Für Live fehlt die Gegenstellen-
Abfrage und das Minimum beider Werte (Festlegung Positionsquelle, Entwurf 10.2) -- B-6
(OKX-Orderpfad) existiert noch nicht, siehe
``app.trading.versandengpass._sende_an_gegenstelle``. In Papier-Betrieb ist das eigene
Buch die einzig geführte Position, es gibt keine davon getrennte Gegenstelle.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.betriebsart import get_betriebsart
from app.dbtypes import utcnow
from app.marketdata.base import DataUnavailable
from app.marketdata.service import MarketService
from app.models import Asset, Portfolio, Position, Proposal, Strategy
from app.models.auto_exit import AutoExitLogEntry
from app.sperrkennzeichen import ist_firmenweit_gesperrt, ist_strategie_gesperrt
from app.trading.proposals import Signal, create_proposal
from app.trading.versandengpass import Versandengpass, VersandOrder

log = logging.getLogger(__name__)

# Entwurf 10.3, CRO-Entscheidung (HED-38/Abschnitt 13): Frist ab Fehlschlag der
# Kursfrische-Prüfung, bis zu der CRO und Board reagieren sollen. Löst selbst keine
# Zeitsteuerung aus (kein Scheduler in diesem Modul) -- die Frist wird nur berechnet
# und protokolliert; die Weiterverfolgung ("wiederholte Meldung, bis ...") ist Sache
# des Aufrufers/Dashboards, nicht dieser Funktion.
ESKALATIONSFRIST = timedelta(minutes=15)

# Order-Seite (VersandOrder.side, "buy"/"sell") -> deutsche Bezeichnung im Protokoll
# (AutoExitLogEntry.order_direction, "kauf"/"verkauf").
_SEITE_ZU_ORDER_RICHTUNG = {"buy": "kauf", "sell": "verkauf"}

# Welche Positionsrichtung eine schließende Order dieser Seite schließt (Entwurf 10.2,
# Schritt A3: "Verkauf bei Long, Kauf bei Short").
_SEITE_SCHLIESST_RICHTUNG = {"sell": "long", "buy": "short"}


class AutoExitFehler(ValueError):
    """Eingabedaten so unvollständig, dass selbst eine Ablehnung nicht sauber gebildet
    werden kann -- ein Programmierfehler beim Aufrufer, kein Marktzustand."""


@dataclass(frozen=True)
class AusstiegsSignal:
    """Eingabe dieses Moduls (Entwurf 10.1/10.6). Entstehung liegt bei der Strategie,
    außerhalb der Ausführungsschicht -- analog zu
    ``app.trading.proposals.Signal`` für Einstiege.
    """

    signal_id: str
    strategy_id: int
    instrument: str
    exchange: str
    symbol: str  # Börsensymbol, z. B. "BTC/USDT" -- an VersandOrder durchgereicht
    side: str  # "buy" | "sell" -- die vom Signal angeforderte, schließende Order-Richtung
    size: Decimal  # angeforderte Schließungsgröße, > 0
    signal_at: datetime
    # Reversal (Entwurf 10.4): von der Strategie ausdrücklich erklärtes Vorhaben einer
    # Gegenposition, unabhängig von `size`. Alle drei Felder zusammen oder keines.
    gegenposition_size: Decimal | None = None
    gegenposition_price: Decimal | None = None
    gegenposition_stop_price: Decimal | None = None


@dataclass(frozen=True)
class AutoExitErgebnis:
    eintrag: AutoExitLogEntry
    gegenposition: Proposal | None = None


async def _offene_position(session: AsyncSession, portfolio_id: int, asset_id: int) -> Decimal:
    """A1/Festlegung Positionsquelle (Entwurf 10.2): in Papier-Betrieb ist das eigene
    Buch die einzig geführte Position. Signiert -- positiv long, negativ short, Null
    keine Position (Konvention wie ``app.trading.stufe2.pruefe_stufe2``)."""
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.asset_id == asset_id)
    )
    return position.quantity if position is not None else Decimal(0)


def _lock_zustand(gesperrt: bool) -> str:
    return "gesetzt" if gesperrt else "offen"


async def fuehre_automatischen_ausstieg_aus(
    session: AsyncSession,
    market: MarketService,
    engpass: Versandengpass,
    signal: AusstiegsSignal,
) -> AutoExitErgebnis:
    """Volle Prüfreihenfolge aus Entwurf 10.6. Schreibt in jedem Fall genau einen
    ``AutoExitLogEntry`` (Kategorie (iv), Bedingung 4/Entwurf 10.5) und committet ihn."""
    if signal.size <= 0:
        raise AutoExitFehler(f"Ausstiegssignal {signal.signal_id} mit size <= 0")

    ist_reversal = signal.gegenposition_size is not None and signal.gegenposition_size > 0
    if ist_reversal and (
        signal.gegenposition_price is None or signal.gegenposition_stop_price is None
    ):
        raise AutoExitFehler(
            f"Ausstiegssignal {signal.signal_id}: gegenposition_size gesetzt, aber "
            "gegenposition_price/-stop_price fehlen -- Teil 2 (Entwurf 10.4) kann ohne "
            "Einstiegsparameter nicht gebildet werden"
        )

    strategie_schluessel = str(signal.strategy_id)

    asset = await session.scalar(select(Asset).where(Asset.symbol == signal.instrument))
    if asset is None:
        raise AutoExitFehler(f"Instrument {signal.instrument!r} nicht im Katalog")

    strategy = await session.get(Strategy, signal.strategy_id)
    if strategy is None or strategy.portfolio_id is None:
        raise AutoExitFehler(
            f"Strategie {signal.strategy_id} ohne Portfolio-Verknüpfung -- offene "
            "Position nicht ermittelbar (A1 fail closed)"
        )
    portfolio = await session.get(Portfolio, strategy.portfolio_id)
    if portfolio is None:
        raise AutoExitFehler(f"Portfolio {strategy.portfolio_id} existiert nicht")

    # Zustand beider Sperrkennzeichen lesen und protokollieren -- für diesen Pfad nicht
    # gatend (Sperrwirkungstabelle, Entwurf 9.3), aber nachvollziehbar (Entwurf 10.6,
    # Hinweis zur Ordergattung: "Die Doppelprüfung wird trotzdem gelesen...").
    firmwide_lock_state = _lock_zustand(await ist_firmenweit_gesperrt(session))
    strategy_lock_state = _lock_zustand(await ist_strategie_gesperrt(session, strategie_schluessel))

    def _eintrag(**felder) -> AutoExitLogEntry:
        basis = dict(
            mode=str(get_betriebsart()),
            strategy_id=signal.strategy_id,
            trigger_signal_id=signal.signal_id,
            trigger_signal_kind="strategie_ausstieg",
            trigger_at=signal.signal_at,
            sent_at=None,
            filled_at=None,
            instrument=signal.instrument,
            exit_size=signal.size,
            order_direction=_SEITE_ZU_ORDER_RICHTUNG[signal.side],
            size_was_capped=False,
            size_capped_to=None,
            is_reversal=ist_reversal,
            reversal_counter_proposal_id=None,
            firmwide_lock_state=firmwide_lock_state,
            strategy_lock_state=strategy_lock_state,
            escalation_at=None,
            escalation_recipients=None,
            escalation_deadline=None,
            escalation_result=None,
        )
        basis.update(felder)
        return AutoExitLogEntry(**basis)

    async def _eskaliere(grund: str, **felder) -> AutoExitErgebnis:
        jetzt = utcnow()
        empfaenger = ["cro", "board"]
        log.warning(
            "BENACHRICHTIGUNG CRO+Board: automatischer Ausstieg eskaliert (%s), "
            "Signal %s, Instrument %s, Frist %s. Es gibt in dieser Installation noch "
            "keinen Benachrichtigungskanal (Dashboard-Scope, Entwurf 5.3/A4, noch "
            "nicht gebaut) -- dieser Log-Eintrag ist die Stelle, an der der echte "
            "Kanal andockt, sobald er existiert (gleiches Muster wie "
            "app.kill_switch._benachrichtige).",
            grund,
            signal.signal_id,
            signal.instrument,
            jetzt + ESKALATIONSFRIST,
        )
        eintrag = _eintrag(
            result="eskaliert_kursfrische",
            escalation_at=jetzt,
            escalation_recipients=empfaenger,
            escalation_deadline=jetzt + ESKALATIONSFRIST,
            escalation_result=grund,
            **felder,
        )
        session.add(eintrag)
        await session.commit()
        return AutoExitErgebnis(eintrag)

    # A1: offene Position lesbar? Ein technischer Lesefehler (nicht: "keine Position",
    # das ist A2) ist fail closed -- Eskalation, keine automatische Order.
    try:
        offene_position = await _offene_position(session, portfolio.id, asset.id)
    except Exception:
        log.exception("A1: offene Position für Strategie %s nicht lesbar", signal.strategy_id)
        return await _eskaliere(
            "position_nicht_lesbar",
            position_before=Decimal(0),
            position_after=None,
            position_direction_closed=_SEITE_SCHLIESST_RICHTUNG[signal.side],
            price_age_seconds=0,
            price_source_timestamp=utcnow(),
        )

    position_direction_closed = _SEITE_SCHLIESST_RICHTUNG[signal.side]
    bestehende_richtung = (
        "long" if offene_position > 0 else "short" if offene_position < 0 else None
    )

    async def _abgelehnt(code: str, **felder) -> AutoExitErgebnis:
        eintrag = _eintrag(
            result=code,
            position_before=offene_position,
            position_after=offene_position,
            position_direction_closed=position_direction_closed,
            price_age_seconds=0,
            price_source_timestamp=utcnow(),
            **felder,
        )
        session.add(eintrag)
        await session.commit()
        return AutoExitErgebnis(eintrag)

    # A2: offene Größe > 0? Ohne offene Position gibt es nichts zu schließen -- kein
    # Fehler, aber auch kein automatischer Ausstieg (Entwurf 10.2, A2).
    if bestehende_richtung is None:
        return await _abgelehnt("abgelehnt_groesse")

    # A3/A6: Richtung der Order ist die schließende für die bestehende Position?
    if bestehende_richtung != position_direction_closed:
        return await _abgelehnt("abgelehnt_richtung")

    # A4: Größe auf die offene Position begrenzen (harte Obergrenze, nie mehr). Jede
    # Überschreitung ist Festlegung-A4-Fall 1 (Positionsgröße hat sich verkleinert) --
    # eine Gegenposition (Fall 2) ist ausschließlich über `gegenposition_size`
    # erklärt, nicht aus dieser Zahl zurückgerechnet (Moduldokumentation oben).
    offene_groesse = abs(offene_position)
    exit_size = signal.size
    size_was_capped = exit_size > offene_groesse
    if size_was_capped:
        exit_size = offene_groesse

    # A5: Größe > 0?
    if exit_size <= 0:
        return await _abgelehnt("abgelehnt_groesse")

    # Kurs für das Protokoll selbst lesen -- "gleiche Quelle" wie die Kursfrische-
    # Prüfung unten (pruefe_stufe2 ruft intern denselben market.view auf), aber hier
    # unabhängig, damit price_age_seconds/price_source_timestamp echte Messwerte sind
    # und keine Platzhalter, auch wenn der Versand-Engpass gleich danach ablehnt.
    try:
        kurs_view = await market.view(asset)
        price_age_seconds = int(kurs_view.age_seconds)
        price_source_timestamp = kurs_view.quote.observed_at
    except DataUnavailable:
        price_age_seconds = 0
        price_source_timestamp = utcnow()

    # Reversal (Entwurf 10.4): Teil 2 "Gegenposition" entsteht unabhängig von Teil 1,
    # als normaler neuer Vorschlag -- Teil 1 läuft unten unverändert weiter, egal ob
    # Teil 2 gelingt, verfällt oder verworfen wird (Festlegung 1).
    gegenposition: Proposal | None = None
    if ist_reversal:
        neue_richtung = "long" if signal.side == "buy" else "short"
        counter_signal = Signal(
            signal_id=f"{signal.signal_id}-gegenposition",
            strategy_id=signal.strategy_id,
            instrument=signal.instrument,
            direction=neue_richtung,
            size=signal.gegenposition_size,
            proposal_price=signal.gegenposition_price,
            stop_price=signal.gegenposition_stop_price,
            proposal_kind="entry",
            signal_at=signal.signal_at,
        )
        gegenposition = await create_proposal(session, counter_signal, mode=str(get_betriebsart()))
        await session.flush()

    # B1-B3 + Versand: dieselbe Stufe-2-Nachprüfung (HED-44) wie jede andere Order,
    # über den Versand-Engpass (HED-42). "Größe erneut gegen offene Position zum
    # Versandzeitpunkt geprüft" (Entwurf 10.6) -- die obige Lesung ist bereits die
    # unmittelbar vor diesem Aufruf, kein zweiter, getrennter Lesevorgang nötig.
    order = VersandOrder(
        gattung="strategie_ausstieg",
        strategie_schluessel=strategie_schluessel,
        exchange=signal.exchange,
        symbol=signal.symbol,
        side=signal.side,
        quantity=exit_size,
        order_type="market",
    )
    vor_versand = utcnow()
    ergebnis = await engpass.send(session, order)

    if not ergebnis.angenommen:
        # pruefe_stufe2 prüft für eine Order ohne Proposal ausschließlich Kursfrische
        # (B1-B3) -- ein Fehlschlag hier ist eindeutig "eskaliert_kursfrische", keine
        # andere Ursache ist auf diesem Pfad erreichbar (siehe Moduldokumentation).
        return await _eskaliere(
            "kursfrische",
            position_before=offene_position,
            position_after=offene_position,
            position_direction_closed=position_direction_closed,
            price_age_seconds=price_age_seconds,
            price_source_timestamp=price_source_timestamp,
            size_was_capped=size_was_capped,
            size_capped_to=exit_size if size_was_capped else None,
        )

    neue_position = offene_position + (exit_size if signal.side == "buy" else -exit_size)
    eintrag = _eintrag(
        result="versandt",
        sent_at=vor_versand,
        position_before=offene_position,
        position_after=neue_position,
        position_direction_closed=position_direction_closed,
        price_age_seconds=price_age_seconds,
        price_source_timestamp=price_source_timestamp,
        size_was_capped=size_was_capped,
        size_capped_to=exit_size if size_was_capped else None,
        reversal_counter_proposal_id=gegenposition.id if gegenposition is not None else None,
    )
    session.add(eintrag)
    await session.commit()
    return AutoExitErgebnis(eintrag, gegenposition)
