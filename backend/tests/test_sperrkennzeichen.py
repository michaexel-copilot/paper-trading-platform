from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen
from app.sperrkennzeichen import (
    doppelpruefung_offen,
    ist_firmenweit_gesperrt,
    ist_strategie_gesperrt,
)


async def test_fehlendes_firmenweites_kennzeichen_ist_fail_closed(session):
    """Testpfad 14, Entwurf 9.6: ein nicht lesbares/nicht vorhandenes Kennzeichen gilt
    als gesetzt. Hier simuliert durch eine Tabelle, die nie initialisiert wurde."""
    assert await ist_firmenweit_gesperrt(session) is True


async def test_offenes_firmenweites_kennzeichen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()
    assert await ist_firmenweit_gesperrt(session) is False


async def test_neue_strategie_ohne_eigene_zeile_ist_offen(session):
    """Entwurf 9.4, Lückenfall: 'noch nie gesetzt' ist ein bestätigtes Ergebnis, kein
    Lesefehler -- anders als beim firmenweiten Kennzeichen oben."""
    assert await ist_strategie_gesperrt(session, "s_neu") is False


async def test_strategie_mit_gesetzter_zeile(session):
    session.add(StrategieSperrkennzeichen(strategie_schluessel="s1", gesetzt=True))
    await session.commit()
    assert await ist_strategie_gesperrt(session, "s1") is True
    assert await ist_strategie_gesperrt(session, "s2") is False


async def test_doppelpruefung_firmenweit_geht_vor(session):
    """Override-Regel 9.4: das firmenweite Kennzeichen übersteuert ein offenes
    Strategie-Kennzeichen, nicht umgekehrt."""
    session.add(FirmenweitesSperrkennzeichen(gesetzt=True))
    session.add(StrategieSperrkennzeichen(strategie_schluessel="s1", gesetzt=False))
    await session.commit()
    assert await doppelpruefung_offen(session, "s1") is False


async def test_doppelpruefung_beide_offen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()
    assert await doppelpruefung_offen(session, "s_beliebig") is True
