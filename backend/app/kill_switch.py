"""Lebenszyklus der zwei Sperrkennzeichen (B-3, Entwurf HED-36 Fassung 1.1 Abschnitt 9.4/9.5).

``app.sperrkennzeichen`` (HED-42) liefert den fail-closed Lesezugriff, auf dem der
Versand-Engpass aufsetzt. Dieses Modul ist die einzige Stelle, die die beiden
Kennzeichen-Tabellen beschreibt -- kein anderer Baustein setzt oder hebt sie auf. Jede
Aktion schreibt zusätzlich eine unveränderliche Protokollzeile (nur anfügen, wie
``ProposalEvent``).

Die Asymmetrie aus Abschnitt 9.5 ist strukturell erzwungen, nicht nur dokumentiert:

- ``setze_firmenweit`` nimmt jeden Auslöser aus ``AUSLOESER`` an, darunter die fünf
  automatischen aus Abschnitt 9.5 -- für diese ist ``entscheider`` optional, weil das
  System selbst setzt.
- ``hebe_firmenweit_auf`` nimmt **ausschließlich** ``board`` als Auslöser (Risikopolitik
  HED-8 Fassung 1.4 Abschnitt 6.2/6.3: "Aufgehoben wird das firmenweite Kennzeichen
  ausschließlich durch das Board ... keine Strategie, kein Agent und kein Prozess darf
  es selbst löschen oder umgehen.") und verlangt zwingend zwei verschiedene namentliche
  Entscheider (Zwei-Personen-Regel, Abnahmepunkt A19, CRO-freigegeben). Es gibt keine
  Funktion, keinen Parameter und keinen Codepfad, der eine automatische Aufhebung
  auslösen könnte.
- ``hebe_strategie_auf`` nimmt weiterhin ``board`` oder ``cro`` als Auslöser (Abschnitt
  6.2: "Aufheben: Stufe 1 der CRO.") -- das betrifft nur das Kennzeichen je Strategie.

Checkpoint im Sinne von Abschnitt 9.2, Festlegung 3: Dieses Modul ist nicht selbst einer
der drei Prüfpunkte. Es verwaltet nur den Zustand, den ``app.sperrkennzeichen`` liest.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dbtypes import utcnow
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen

log = logging.getLogger(__name__)

# Entwurf 9.1: wer das firmenweite Kennzeichen setzen darf -- Board/CRO manuell, dazu
# die fünf automatischen Auslöser aus Abschnitt 9.5.
FIRMENWEIT_SETZEN_AUSLOESER = frozenset(
    {
        "board",
        "cro",
        "stufe2",
        "auflagen_selbsttest_fehlgeschlagen",
        "betriebsart_abweichung",
        "order_ohne_versandengpass_eintrag",
        "protokoll_nicht_schreibbar",
        "limitliste_nicht_lesbar",
    }
)
# Entwurf 9.1: wer das Kennzeichen einer einzelnen Strategie setzen darf. Keiner der
# fünf automatischen 9.5-Auslöser gehört hierher -- die sind ausdrücklich Auslöser des
# *firmenweiten* Kennzeichens.
STRATEGIE_SETZEN_AUSLOESER = frozenset({"board", "cro", "stufe1"})
# Risikopolitik HED-8 Fassung 1.4 Abschnitt 6.2/6.3 (Nebenbefund HED-105): das
# firmenweite Kennzeichen hebt ausschließlich das Board auf -- kein Agent, kein
# Prozess, auch nicht der CRO. Keine Systemlogik, kein automatischer Auslöser.
FIRMENWEIT_AUFHEBEN_AUSLOESER = frozenset({"board"})
# Abschnitt 6.2: das Kennzeichen je Strategie hebt zusätzlich der CRO auf (Stufe 1).
STRATEGIE_AUFHEBEN_AUSLOESER = frozenset({"board", "cro"})


class ZweiPersonenRegelVerletzt(ValueError):
    """Aufhebung des firmenweiten Kennzeichens ohne zwei verschiedene Entscheider."""


def _benachrichtige(kennzeichen_typ: str, aktion: str, strategie_schluessel: str | None) -> None:
    """Entwurf 9.4: 'An CRO und Board, sofort, bei Setzen und bei Aufheben.'

    Es gibt in dieser Installation noch keinen Benachrichtigungskanal (E-Mail-Versand
    ist Dashboard-Scope, Abschnitt 5.3/A4, noch nicht gebaut). Dieser Hook ist die
    Stelle, an der der echte Kanal andockt, sobald er existiert -- bis dahin ein klar
    markierter Log-Eintrag, damit die Pflicht nicht unsichtbar verschwindet.
    """
    ziel = f"Strategie {strategie_schluessel}" if strategie_schluessel else "firmenweit"
    log.warning(
        "BENACHRICHTIGUNG CRO+Board: Sperrkennzeichen %s %s (%s)", ziel, aktion, kennzeichen_typ
    )


async def _protokolliere(
    session: AsyncSession,
    *,
    kennzeichen_typ: str,
    strategie_schluessel: str | None,
    aktion: str,
    ausloeser: str,
    begruendung: str,
    entscheider_1: str | None,
    entscheider_2: str | None = None,
) -> None:
    session.add(
        SperrkennzeichenProtokoll(
            kennzeichen_typ=kennzeichen_typ,
            strategie_schluessel=strategie_schluessel,
            aktion=aktion,
            ausloeser=ausloeser,
            entscheider_1=entscheider_1,
            entscheider_2=entscheider_2,
            begruendung=begruendung,
            occurred_at=utcnow(),
        )
    )


async def setze_firmenweit(
    session: AsyncSession,
    *,
    ausloeser: str,
    begruendung: str,
    entscheider: str | None = None,
) -> None:
    if ausloeser not in FIRMENWEIT_SETZEN_AUSLOESER:
        raise ValueError(f"Unbekannter oder unzulässiger Auslöser für Setzen: {ausloeser!r}")
    if ausloeser in ("board", "cro") and not (entscheider and entscheider.strip()):
        raise ValueError("Manuelles Setzen verlangt eine namentliche Entscheider-Kennung.")
    if not begruendung or not begruendung.strip():
        raise ValueError("Begründung ist Pflicht (Entwurf 9.4).")

    zeile = await session.scalar(select(FirmenweitesSperrkennzeichen).limit(1))
    if zeile is None:
        zeile = FirmenweitesSperrkennzeichen(gesetzt=True)
        session.add(zeile)
    else:
        zeile.gesetzt = True

    await _protokolliere(
        session,
        kennzeichen_typ="firmenweit",
        strategie_schluessel=None,
        aktion="gesetzt",
        ausloeser=ausloeser,
        begruendung=begruendung,
        entscheider_1=entscheider,
    )
    await session.commit()
    _benachrichtige("firmenweit", "gesetzt", None)


async def hebe_firmenweit_auf(
    session: AsyncSession,
    *,
    entscheider_1: str,
    entscheider_2: str,
    begruendung: str,
    ausloeser: str = "board",
) -> None:
    """Einziger Aufhebungsweg für das firmenweite Kennzeichen (Entwurf 9.4).

    Nur das Board darf aufheben (Risikopolitik HED-8 Fassung 1.4 Abschnitt 6.2/6.3) --
    der CRO ist hier bewusst ausgeschlossen, anders als bei ``hebe_strategie_auf``.
    Zwei-Personen-Regel (A19, CRO-freigegeben): zwei verschiedene namentliche
    Entscheider sind Pflicht. Ein Verstoß ändert den Zustand nicht -- fail closed, die
    Sperre bleibt gesetzt, bis eine gültige Aufhebung vorliegt.
    """
    if ausloeser not in FIRMENWEIT_AUFHEBEN_AUSLOESER:
        raise ValueError(f"Unzulässiger Auslöser für Aufhebung: {ausloeser!r}")
    e1 = (entscheider_1 or "").strip()
    e2 = (entscheider_2 or "").strip()
    if not e1 or not e2:
        raise ZweiPersonenRegelVerletzt("Zwei namentliche Entscheider sind Pflicht.")
    if e1.lower() == e2.lower():
        raise ZweiPersonenRegelVerletzt(
            "Zwei-Personen-Regel: entscheider_1 und entscheider_2 müssen verschiedene "
            "Personen sein."
        )
    if not begruendung or not begruendung.strip():
        raise ValueError("Begründung ist Pflicht (Entwurf 9.4).")

    zeile = await session.scalar(select(FirmenweitesSperrkennzeichen).limit(1))
    if zeile is None:
        raise ValueError("Kein firmenweites Kennzeichen vorhanden -- nichts aufzuheben.")
    zeile.gesetzt = False

    await _protokolliere(
        session,
        kennzeichen_typ="firmenweit",
        strategie_schluessel=None,
        aktion="aufgehoben",
        ausloeser=ausloeser,
        begruendung=begruendung,
        entscheider_1=e1,
        entscheider_2=e2,
    )
    await session.commit()
    _benachrichtige("firmenweit", "aufgehoben", None)


async def setze_strategie(
    session: AsyncSession,
    strategie_schluessel: str,
    *,
    ausloeser: str,
    begruendung: str,
    entscheider: str | None = None,
) -> None:
    if ausloeser not in STRATEGIE_SETZEN_AUSLOESER:
        raise ValueError(f"Unbekannter oder unzulässiger Auslöser für Setzen: {ausloeser!r}")
    if ausloeser in ("board", "cro") and not (entscheider and entscheider.strip()):
        raise ValueError("Manuelles Setzen verlangt eine namentliche Entscheider-Kennung.")
    if not begruendung or not begruendung.strip():
        raise ValueError("Begründung ist Pflicht (Entwurf 9.4).")

    zeile = await session.scalar(
        select(StrategieSperrkennzeichen).where(
            StrategieSperrkennzeichen.strategie_schluessel == strategie_schluessel
        )
    )
    if zeile is None:
        zeile = StrategieSperrkennzeichen(strategie_schluessel=strategie_schluessel, gesetzt=True)
        session.add(zeile)
    else:
        zeile.gesetzt = True

    await _protokolliere(
        session,
        kennzeichen_typ="strategie",
        strategie_schluessel=strategie_schluessel,
        aktion="gesetzt",
        ausloeser=ausloeser,
        begruendung=begruendung,
        entscheider_1=entscheider,
    )
    await session.commit()
    _benachrichtige("strategie", "gesetzt", strategie_schluessel)


async def hebe_strategie_auf(
    session: AsyncSession,
    strategie_schluessel: str,
    *,
    entscheider: str,
    begruendung: str,
    ausloeser: str = "board",
) -> None:
    """Board oder CRO heben das Strategie-Kennzeichen auf (Entwurf 9.1-Tabelle,
    Risikopolitik HED-8 Fassung 1.4 Abschnitt 6.2: "Aufheben: Stufe 1 der CRO.").

    Keine Zwei-Personen-Regel hier -- A19 beschränkt sie ausdrücklich auf die
    Aufhebung des firmenweiten Kennzeichens, als risikoreichste Einzelaktion.
    """
    if ausloeser not in STRATEGIE_AUFHEBEN_AUSLOESER:
        raise ValueError(f"Unzulässiger Auslöser für Aufhebung: {ausloeser!r}")
    if not entscheider or not entscheider.strip():
        raise ValueError("Eine namentliche Entscheider-Kennung ist Pflicht.")
    if not begruendung or not begruendung.strip():
        raise ValueError("Begründung ist Pflicht (Entwurf 9.4).")

    zeile = await session.scalar(
        select(StrategieSperrkennzeichen).where(
            StrategieSperrkennzeichen.strategie_schluessel == strategie_schluessel
        )
    )
    if zeile is None:
        raise ValueError(
            f"Kein Sperrkennzeichen für Strategie {strategie_schluessel!r} vorhanden -- "
            "nichts aufzuheben."
        )
    zeile.gesetzt = False

    await _protokolliere(
        session,
        kennzeichen_typ="strategie",
        strategie_schluessel=strategie_schluessel,
        aktion="aufgehoben",
        ausloeser=ausloeser,
        begruendung=begruendung,
        entscheider_1=entscheider.strip(),
    )
    await session.commit()
    _benachrichtige("strategie", "aufgehoben", strategie_schluessel)
