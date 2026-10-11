"""Lebenszyklus der zwei Sperrkennzeichen (HED-47, Entwurf HED-36 Fassung 1.1
Abschnitt 9.4/9.5): Setzen, Aufheben, Zwei-Personen-Regel (A19), Protokoll.

Die reine Lesesemantik (fail closed, Doppelprüfung, Override) ist
test_sperrkennzeichen.py und test_versandengpass.py Sache -- hier geht es nur um die
Aktionen, die den Zustand verändern.
"""

import pytest
from sqlalchemy import select

from app.kill_switch import (
    ZweiPersonenRegelVerletzt,
    hebe_firmenweit_auf,
    hebe_strategie_auf,
    setze_firmenweit,
    setze_strategie,
)
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.models.sperrkennzeichen import StrategieSperrkennzeichen
from app.sperrkennzeichen import (
    doppelpruefung_offen,
    ist_firmenweit_gesperrt,
    ist_strategie_gesperrt,
)


async def _protokoll(session, **filters) -> list[SperrkennzeichenProtokoll]:
    stmt = select(SperrkennzeichenProtokoll)
    for key, value in filters.items():
        stmt = stmt.where(getattr(SperrkennzeichenProtokoll, key) == value)
    return list(await session.scalars(stmt))


# --- Firmenweit: Setzen -----------------------------------------------------------------


async def test_setze_firmenweit_manuell_verlangt_entscheider(session):
    with pytest.raises(ValueError):
        await setze_firmenweit(session, ausloeser="board", begruendung="Testgrund")


async def test_setze_firmenweit_manuell_schreibt_zeile_und_sperrt(session):
    await setze_firmenweit(
        session, ausloeser="cro", begruendung="Marktanomalie", entscheider="cro.mueller"
    )
    assert await ist_firmenweit_gesperrt(session) is True
    zeilen = await _protokoll(session, kennzeichen_typ="firmenweit", aktion="gesetzt")
    assert len(zeilen) == 1
    assert zeilen[0].entscheider_1 == "cro.mueller"
    assert zeilen[0].begruendung == "Marktanomalie"


async def test_setze_firmenweit_automatischer_ausloeser_ohne_entscheider_erlaubt(session):
    """Entwurf 9.5: die fünf automatischen Auslöser setzen ohne menschlichen Entscheider."""
    await setze_firmenweit(
        session,
        ausloeser="betriebsart_abweichung",
        begruendung="Prozess-Betriebsart passt nicht zur Datenbank-Markierung",
    )
    assert await ist_firmenweit_gesperrt(session) is True
    zeilen = await _protokoll(session, kennzeichen_typ="firmenweit", aktion="gesetzt")
    assert zeilen[0].entscheider_1 is None
    assert zeilen[0].ausloeser == "betriebsart_abweichung"


async def test_setze_firmenweit_unbekannter_ausloeser_wird_abgelehnt(session):
    with pytest.raises(ValueError):
        await setze_firmenweit(session, ausloeser="laune", begruendung="x", entscheider="a")


async def test_setze_firmenweit_ohne_begruendung_wird_abgelehnt(session):
    with pytest.raises(ValueError):
        await setze_firmenweit(session, ausloeser="board", begruendung="  ", entscheider="a")


# --- Firmenweit: Aufheben, Zwei-Personen-Regel (A19) ------------------------------------


async def test_hebe_firmenweit_auf_ohne_vorhandenes_kennzeichen_schlaegt_fehl(session):
    with pytest.raises(ValueError):
        await hebe_firmenweit_auf(
            session, entscheider_1="a", entscheider_2="b", begruendung="Lage beruhigt"
        )


async def test_hebe_firmenweit_auf_mit_zwei_verschiedenen_entscheidern(session):
    await setze_firmenweit(session, ausloeser="cro", begruendung="Stufe 2", entscheider="cro.m")
    await hebe_firmenweit_auf(
        session, entscheider_1="cro.mueller", entscheider_2="ceo.schmidt", begruendung="Geprüft"
    )
    assert await ist_firmenweit_gesperrt(session) is False
    zeilen = await _protokoll(session, kennzeichen_typ="firmenweit", aktion="aufgehoben")
    assert len(zeilen) == 1
    assert zeilen[0].entscheider_1 == "cro.mueller"
    assert zeilen[0].entscheider_2 == "ceo.schmidt"


async def test_hebe_firmenweit_auf_mit_gleichem_entscheider_zweimal_bleibt_gesperrt(session):
    await setze_firmenweit(session, ausloeser="cro", begruendung="Stufe 2", entscheider="cro.m")
    with pytest.raises(ZweiPersonenRegelVerletzt):
        await hebe_firmenweit_auf(
            session,
            entscheider_1="cro.mueller",
            entscheider_2="cro.mueller",
            begruendung="Alleine entschieden",
        )
    # Fail closed: kein Teilzustand, die Sperre bleibt unverändert gesetzt.
    assert await ist_firmenweit_gesperrt(session) is True
    assert await _protokoll(session, kennzeichen_typ="firmenweit", aktion="aufgehoben") == []


async def test_hebe_firmenweit_auf_case_insensitive_name_wird_erkannt(session):
    """'CRO.Mueller' und 'cro.mueller' sind dieselbe Person -- die Regel lässt sich
    nicht durch Schreibweise umgehen."""
    await setze_firmenweit(session, ausloeser="board", begruendung="x", entscheider="a")
    with pytest.raises(ZweiPersonenRegelVerletzt):
        await hebe_firmenweit_auf(
            session, entscheider_1="CRO.Mueller", entscheider_2="cro.mueller", begruendung="x"
        )


async def test_hebe_firmenweit_auf_ohne_begruendung_schlaegt_fehl(session):
    await setze_firmenweit(session, ausloeser="board", begruendung="x", entscheider="a")
    with pytest.raises(ValueError):
        await hebe_firmenweit_auf(session, entscheider_1="a", entscheider_2="b", begruendung="")
    assert await ist_firmenweit_gesperrt(session) is True


async def test_hebe_firmenweit_auf_mit_automatischem_ausloeser_wird_abgelehnt(session):
    """Entwurf 9.5, letzter Satz: 'Automatisches Aufheben ist in keinem Fall erlaubt.'"""
    await setze_firmenweit(session, ausloeser="board", begruendung="x", entscheider="a")
    with pytest.raises(ValueError):
        await hebe_firmenweit_auf(
            session,
            entscheider_1="a",
            entscheider_2="b",
            begruendung="x",
            ausloeser="betriebsart_abweichung",
        )
    assert await ist_firmenweit_gesperrt(session) is True


# --- Strategie: Setzen und Aufheben, keine Zwei-Personen-Regel -------------------------


async def test_setze_strategie_stufe1_ohne_entscheider_erlaubt(session):
    await setze_strategie(session, "s1", ausloeser="stufe1", begruendung="Limitbruch")
    assert await ist_strategie_gesperrt(session, "s1") is True
    assert await ist_strategie_gesperrt(session, "s2") is False


async def test_setze_strategie_automatischer_firmenweiter_ausloeser_unzulaessig(session):
    """Die fünf 9.5-Auslöser gehören zum firmenweiten, nicht zum Strategie-Kennzeichen."""
    with pytest.raises(ValueError):
        await setze_strategie(session, "s1", ausloeser="betriebsart_abweichung", begruendung="x")


async def test_hebe_strategie_auf_mit_einem_entscheider(session):
    await setze_strategie(session, "s1", ausloeser="stufe1", begruendung="Limitbruch")
    await hebe_strategie_auf(session, "s1", entscheider="cro.mueller", begruendung="geprüft")
    assert await ist_strategie_gesperrt(session, "s1") is False
    zeilen = await _protokoll(
        session, kennzeichen_typ="strategie", strategie_schluessel="s1", aktion="aufgehoben"
    )
    assert zeilen[0].entscheider_1 == "cro.mueller"
    assert zeilen[0].entscheider_2 is None


async def test_hebe_strategie_auf_ohne_vorhandenes_kennzeichen_schlaegt_fehl(session):
    with pytest.raises(ValueError):
        await hebe_strategie_auf(session, "s_unbekannt", entscheider="a", begruendung="x")


async def test_setze_strategie_zweimal_schreibt_zwei_protokollzeilen(session):
    """Jeder Auslöser wird protokolliert, auch wenn das Kennzeichen bereits gesetzt war."""
    await setze_strategie(session, "s1", ausloeser="stufe1", begruendung="erster Bruch")
    await setze_strategie(
        session, "s1", ausloeser="cro", begruendung="zweiter Grund", entscheider="x"
    )
    zeilen = await _protokoll(
        session, kennzeichen_typ="strategie", strategie_schluessel="s1", aktion="gesetzt"
    )
    assert len(zeilen) == 2


# --- Override bleibt nach Setzen/Aufheben korrekt (Regressionsschutz zu 9.4) -----------


async def test_firmenweit_gesetzt_uebersteuert_offenes_strategie_kennzeichen(session):
    session.add(StrategieSperrkennzeichen(strategie_schluessel="s1", gesetzt=False))
    await session.flush()
    await setze_firmenweit(session, ausloeser="cro", begruendung="Stufe 2", entscheider="cro.m")
    assert await ist_firmenweit_gesperrt(session) is True
    assert await ist_strategie_gesperrt(session, "s1") is False
    assert await doppelpruefung_offen(session, "s1") is False
