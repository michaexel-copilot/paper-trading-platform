"""HED-55: laufende Prüfung während des Betriebs (Entwurf Abschnitt 3.2, "Laufende
Prüfung, nicht nur beim Start"). Testet die einzelne Durchführung (``pruefe_einmal``),
nicht die Endlos-Schleife in ``app.main`` -- derselbe Zuschnitt wie bei
``check_all_assets`` / ``verfalle_ueberfaellige_vorschlaege`` (siehe test_catalog.py /
test_proposals.py)."""

from sqlalchemy import select

import app.schluesseltrennung_laufend as laufend
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen


async def test_pruefe_einmal_setzt_sperrkennzeichen_bei_fehlschlag(session_factory, monkeypatch):
    async def fake_bestanden():
        return False

    monkeypatch.setattr(laufend, "selbsttest_bestanden", fake_bestanden)

    bestanden = await laufend.pruefe_einmal(session_factory)

    assert bestanden is False
    async with session_factory() as session:
        zeile = await session.scalar(select(FirmenweitesSperrkennzeichen).limit(1))
        assert zeile is not None
        assert zeile.gesetzt is True
        protokoll = await session.scalar(select(SperrkennzeichenProtokoll).limit(1))
        assert protokoll.ausloeser == "auflagen_selbsttest_fehlgeschlagen"
        assert protokoll.kennzeichen_typ == "firmenweit"


async def test_pruefe_einmal_setzt_kennzeichen_nicht_bei_erfolg(session_factory, monkeypatch):
    async def fake_bestanden():
        return True

    monkeypatch.setattr(laufend, "selbsttest_bestanden", fake_bestanden)

    bestanden = await laufend.pruefe_einmal(session_factory)

    assert bestanden is True
    async with session_factory() as session:
        zeile = await session.scalar(select(FirmenweitesSperrkennzeichen).limit(1))
        assert zeile is None
