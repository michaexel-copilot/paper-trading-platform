"""Zweiphasige Slippage-Messung: Berechnung (Entwurf HED-36 Fassung 1.1 Abschnitt 11).

Reine Berechnung aus bereits anderswo erfassten Zeitstempeln und Preisen (Signal/
Entscheidung: HED-41, Versand: HED-42, Füllung/Füllpreis: Fill-Nachweis der Gegenstelle --
noch nicht verdrahtet, B-6/Stufe-2-Nachprüfung liefern Füllpreis und `filled_at`/`sent_at`
erst mit dem echten Orderpfad). Dieses Modul nimmt den Füllpreis deshalb bewusst als
expliziten Parameter entgegen, statt ihn aus einer noch nicht bestehenden
Order/Trade-Verknüpfung zu lesen.

Phase 1 und Phase 2 werden getrennt berechnet und getrennt gespeichert (Festlegung
11.2). Es gibt absichtlich keine Funktion, die beide zu einer Gesamtzahl addiert.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.proposals import (
    STATE_GEFUELLT,
    STATE_TEILGEFUELLT_BEENDET,
    STATE_VON_GEGENSTELLE_ABGELEHNT,
    Proposal,
)
from app.models.slippage import SlippageMessungEintrag


class MessungNichtMoeglich(ValueError):
    """Ein Zeitstempel oder Preis, den die Messung an dieser Stelle voraussetzt, fehlt."""


@dataclass(frozen=True)
class Phase1Messung:
    dauer_sekunden: int
    kursveraenderung: Decimal | None  # proposal_price -> limit_price, informativ (Entwurf 11.1)


@dataclass(frozen=True)
class Phase2Messung:
    dauer_freigabe_bis_versand_sekunden: int
    dauer_versand_bis_fuellung_sekunden: int
    dauer_freigabe_bis_fuellung_sekunden: int
    referenzkurs: Decimal
    preisabweichung_absolut: Decimal
    preisabweichung_prozentpunkte: Decimal
    preisabweichung_risikoeinheiten: Decimal


def berechne_phase1(proposal: Proposal) -> Phase1Messung:
    """Signal -> Freigabe (Entwurf 11.1/11.3). Braucht nur signal_at und decided_at.

    Nicht gegen den Paper-Wert verglichen (Festlegung 11.2) -- das ist hier bewusst
    keine Eigenschaft dieser Funktion, sondern eine Eigenschaft des Aufrufers: nichts in
    ``Phase1Messung`` fließt in ``berechne_phase2`` oder umgekehrt ein.
    """
    if proposal.decided_at is None:
        raise MessungNichtMoeglich(
            "Phase 1 braucht decided_at (Vorschlag noch nicht entschieden)."
        )
    dauer = (proposal.decided_at - proposal.signal_at).total_seconds()
    kursveraenderung = (
        proposal.limit_price - proposal.proposal_price
        if proposal.limit_price is not None
        else None
    )
    return Phase1Messung(dauer_sekunden=round(dauer), kursveraenderung=kursveraenderung)


def berechne_phase2(proposal: Proposal, fuellpreis: Decimal) -> Phase2Messung:
    """Freigabe -> Füllung (Entwurf 11.1/11.3/11.4).

    Hauptwerte im Paper-Vergleich (Tabelle 11.5): dauer_freigabe_bis_fuellung_sekunden
    und preisabweichung_risikoeinheiten.
    """
    fehlend = [
        name
        for name, wert in (
            ("decided_at", proposal.decided_at),
            ("sent_at", proposal.sent_at),
            ("filled_at", proposal.filled_at),
            ("limit_price", proposal.limit_price),
        )
        if wert is None
    ]
    if fehlend:
        raise MessungNichtMoeglich(f"Phase 2 braucht {', '.join(fehlend)}, fehlt hier.")

    referenzkurs = proposal.limit_price
    if proposal.direction == "long":
        preisabweichung_absolut = fuellpreis - referenzkurs
    elif proposal.direction == "short":
        preisabweichung_absolut = referenzkurs - fuellpreis
    else:
        raise MessungNichtMoeglich(f"Unbekannte Richtung {proposal.direction!r}.")

    preisabweichung_prozentpunkte = (preisabweichung_absolut / referenzkurs) * 100
    preisabweichung_risikoeinheiten = preisabweichung_absolut / proposal.stop_distance

    return Phase2Messung(
        dauer_freigabe_bis_versand_sekunden=round(
            (proposal.sent_at - proposal.decided_at).total_seconds()
        ),
        dauer_versand_bis_fuellung_sekunden=round(
            (proposal.filled_at - proposal.sent_at).total_seconds()
        ),
        dauer_freigabe_bis_fuellung_sekunden=round(
            (proposal.filled_at - proposal.decided_at).total_seconds()
        ),
        referenzkurs=referenzkurs,
        preisabweichung_absolut=preisabweichung_absolut,
        preisabweichung_prozentpunkte=preisabweichung_prozentpunkte,
        preisabweichung_risikoeinheiten=preisabweichung_risikoeinheiten,
    )


async def erfasse_messung(
    session: AsyncSession, proposal: Proposal, *, fuellpreis: Decimal
) -> SlippageMessungEintrag:
    """Schreibt genau eine unveränderliche Messzeile für diesen Vorschlag.

    Aufrufer ist die noch zu bauende Fill-Nachweis-Verarbeitung (B-6/Stufe-2-
    Nachprüfung) -- diese Funktion setzt weder filled_at/sent_at noch verknüpft sie
    selbst einen Trade. Sie berechnet und speichert nur, was aus einem bereits
    entschiedenen, versandten und gefüllten Vorschlag plus dem gemeldeten Füllpreis
    folgt (Entwurf Abschnitt 11). Ein zweiter Aufruf für denselben Vorschlag schlägt an
    der Unique-Constraint auf ``proposal_id`` fehl, statt die erste Messung stillschweigend
    zu überschreiben.
    """
    if proposal.state not in (STATE_GEFUELLT, STATE_TEILGEFUELLT_BEENDET):
        raise MessungNichtMoeglich(
            f"Vorschlag {proposal.id} ist im Zustand {proposal.state!r}, nicht gefüllt "
            "oder teilgefüllt beendet -- keine Messung ohne tatsächlichen Füllpreis."
        )
    phase1 = berechne_phase1(proposal)
    phase2 = berechne_phase2(proposal, fuellpreis)
    eintrag = SlippageMessungEintrag(
        proposal_id=proposal.id,
        dauer_signal_bis_freigabe_sekunden=phase1.dauer_sekunden,
        kursveraenderung_signal_bis_freigabe=phase1.kursveraenderung,
        dauer_freigabe_bis_versand_sekunden=phase2.dauer_freigabe_bis_versand_sekunden,
        dauer_versand_bis_fuellung_sekunden=phase2.dauer_versand_bis_fuellung_sekunden,
        dauer_freigabe_bis_fuellung_sekunden=phase2.dauer_freigabe_bis_fuellung_sekunden,
        fuellpreis=fuellpreis,
        referenzkurs=phase2.referenzkurs,
        preisabweichung_absolut=phase2.preisabweichung_absolut,
        preisabweichung_prozentpunkte=phase2.preisabweichung_prozentpunkte,
        preisabweichung_risikoeinheiten=phase2.preisabweichung_risikoeinheiten,
    )
    session.add(eintrag)
    await session.commit()
    return eintrag


# Terminal-Folgezustände von "versandt" (Tabelle 4.2) -- die Kohorte für die Füllquote.
# Ein Vorschlag, der noch in "versandt" steht (Order noch offen), gehört noch nicht zur
# Kohorte: er ist weder gefüllt noch endgültig nicht gefüllt, und würde die Quote als
# verfrühter Fehlschlag verzerren.
_FUELLQUOTE_KOHORTE_ZUSTAENDE = frozenset(
    {STATE_GEFUELLT, STATE_TEILGEFUELLT_BEENDET, STATE_VON_GEGENSTELLE_ABGELEHNT}
)


def berechne_fuellquote(proposals) -> Decimal:
    """Füllquote über eine Kohorte abgeschlossener, versandter Vorschläge (Entwurf 11.4,
    Pflichtangabe neben der Phase-2-Slippage: ohne sie wäre eine niedrige Slippage bei
    vielen Nichtausführungen eine Scheinverbesserung).

    Nur Vorschläge, die den Versand-Engpass tatsächlich passiert haben und seither einen
    Endzustand erreicht haben, zählen zur Kohorte -- ein wegen Limit/Freigabe/
    Sperrkennzeichen vor dem Versand abgelehnter Vorschlag ist kein Nichtausführungsfall
    der Ausführungstechnik und würde die Quote verzerren.
    """
    kohorte = [p for p in proposals if p.state in _FUELLQUOTE_KOHORTE_ZUSTAENDE]
    if not kohorte:
        raise MessungNichtMoeglich(
            "Keine abgeschlossenen, versandten Vorschläge in dieser Kohorte."
        )
    gefuellt = sum(1 for p in kohorte if p.state == STATE_GEFUELLT)
    return Decimal(gefuellt) / Decimal(len(kohorte))
