from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen, StrategieSperrkennzeichen
from app.models.versandengpass import VersandEngpassEintrag
from app.trading.versandengpass import Versandengpass, VersandOrder


def _order(**overrides) -> VersandOrder:
    felder = dict(
        gattung="einstieg",
        strategie_schluessel="s1",
        exchange="okx",
        symbol="BTC/USDT",
        side="buy",
        quantity=Decimal("0.01"),
        order_type="market",
    )
    felder.update(overrides)
    return VersandOrder(**felder)


async def _setze_firmenweit(session, gesetzt: bool) -> None:
    zeile = await session.scalar(select(FirmenweitesSperrkennzeichen))
    zeile.gesetzt = gesetzt
    await session.commit()


@pytest.fixture
async def mit_schluessel(betriebsart, monkeypatch, session):
    betriebsart("papier")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("OKX_DEMO_API_SECRET", "demo-secret")
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    # Normalzustand einer laufenden Installation: das firmenweite Kennzeichen existiert
    # und ist offen. Ein fehlendes Kennzeichen ist fail-closed (test_sperrkennzeichen.py)
    # -- das wird hier bewusst nicht als Ausgangslage für die despatch-Logik-Tests
    # verwendet, sonst würde jeder einzelne Test diesen einen Fall erneut beweisen.
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


async def test_ohne_schluessel_wird_abgelehnt(betriebsart, monkeypatch, session):
    betriebsart("papier")
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("OKX_LIVE_API_KEY", raising=False)
    ergebnis = await Versandengpass().send(session, _order(gattung="stornierung"))
    assert ergebnis.angenommen is False
    assert ergebnis.ablehnungsgrund == "schluessel_nicht_verwendbar"


async def test_unbekannte_gattung_wird_abgelehnt(mit_schluessel, session):
    ergebnis = await Versandengpass().send(session, _order(gattung="irgendwas"))
    assert ergebnis.angenommen is False
    assert ergebnis.ablehnungsgrund == "unbekannte_gattung"


async def test_einstieg_ohne_freigabe_nachweis_wird_abgelehnt(mit_schluessel, session):
    """Fail-closed Standard: ohne verdrahteten Freigabe-Nachweis-Prüfer (B-2) geht nie
    eine Einstiegsorder durch, selbst wenn alle anderen Prüfungen bestehen würden."""
    ergebnis = await Versandengpass().send(session, _order(gattung="einstieg"))
    assert ergebnis.angenommen is False
    assert ergebnis.ablehnungsgrund == "kein_gueltiger_freigabe_nachweis"


async def test_stufe2_fail_closed_blockiert_auch_mit_freigabe_nachweis(mit_schluessel, session):
    async def immer_gueltig(_session, _order):
        return True

    engpass = Versandengpass(freigabe_nachweis_pruefer=immer_gueltig)
    ergebnis = await engpass.send(session, _order(gattung="einstieg"))
    assert ergebnis.angenommen is False
    assert ergebnis.ablehnungsgrund == "stufe2_nicht_bestanden"


async def test_firmenweites_kennzeichen_blockiert_einstieg(mit_schluessel, session):
    await _setze_firmenweit(session, True)

    async def immer_gueltig(_session, _order):
        return True

    engpass = Versandengpass(freigabe_nachweis_pruefer=immer_gueltig, stufe2_pruefer=immer_gueltig)
    ergebnis = await engpass.send(session, _order(gattung="einstieg"))
    assert ergebnis.angenommen is False
    assert ergebnis.ablehnungsgrund == "unterdrueckt_durch_sperre"


async def test_strategie_kennzeichen_blockiert_andere_strategie_nicht(mit_schluessel, session):
    """B-3-Wirkbereich: ein Strategie-Kennzeichen hält nur die eigene Strategie an.

    Prüft die Vorprüfung direkt (``_pruefe``), nicht den vollen ``send()``: mit allen
    anderen Prüfungen künstlich auf 'bestanden' gesetzt würde eine offene Strategie bis
    zum noch nicht gebauten Gegenstellen-Versand durchlaufen (B-6, siehe eigener Test
    weiter unten) -- hier geht es ausschließlich um die Sperrwirkung selbst.
    """
    session.add(StrategieSperrkennzeichen(strategie_schluessel="s1", gesetzt=True))
    await session.commit()

    engpass = Versandengpass()
    gesperrt = await engpass._pruefe(session, _order(gattung="einstieg", strategie_schluessel="s1"))
    offen = await engpass._pruefe(session, _order(gattung="einstieg", strategie_schluessel="s2"))
    assert gesperrt == "unterdrueckt_durch_sperre"
    assert offen != "unterdrueckt_durch_sperre"


async def test_neue_strategie_waehrend_firmenweiter_sperre_bleibt_blockiert(
    mit_schluessel, session
):
    """Testpfad 4 aus Entwurf 9.6, Gruppe B (Lückenfall): eine neue Strategie ohne
    eigenes Sperrkennzeichen bleibt blockiert, weil das firmenweite Kennzeichen zuerst
    gelesen wird und übersteuert."""
    await _setze_firmenweit(session, True)

    ergebnis = await Versandengpass().send(
        session, _order(gattung="einstieg", strategie_schluessel="s_neu_ohne_eigenes_kennzeichen")
    )
    assert ergebnis.angenommen is False
    assert ergebnis.ablehnungsgrund == "unterdrueckt_durch_sperre"


async def test_stornierung_laeuft_trotz_firmenweiter_sperre(mit_schluessel, session):
    """Sperrwirkungstabelle 9.3: Stornierung senkt Risiko und bleibt immer erlaubt."""
    await _setze_firmenweit(session, True)

    ergebnis = await Versandengpass().send(session, _order(gattung="stornierung"))
    # Stornierung braucht keinen Freigabe-Nachweis, scheitert aber am fail-closed
    # Stufe-2-Standard -- das zeigt, dass sie die Sperre selbst nicht erreicht.
    assert ergebnis.ablehnungsgrund != "unterdrueckt_durch_sperre"


async def test_stop_platzierung_laeuft_trotz_strategie_sperre(mit_schluessel, session):
    session.add(StrategieSperrkennzeichen(strategie_schluessel="s1", gesetzt=True))
    await session.commit()

    ergebnis = await Versandengpass().send(session, _order(gattung="stop_platzierung"))
    assert ergebnis.ablehnungsgrund != "unterdrueckt_durch_sperre"


async def test_alle_pruefungen_bestanden_aber_noch_kein_echter_versand(mit_schluessel, session):
    """B-6 ist noch nicht gebaut: selbst wenn alle sieben Prüfungen bestehen, gibt es
    keinen Code, der eine Order tatsächlich an die Gegenstelle schickt."""

    async def immer_gueltig(_session, _order):
        return True

    engpass = Versandengpass(freigabe_nachweis_pruefer=immer_gueltig, stufe2_pruefer=immer_gueltig)
    with pytest.raises(NotImplementedError):
        await engpass.send(session, _order(gattung="einstieg"))


async def test_jede_entscheidung_wird_protokolliert(mit_schluessel, session):
    await Versandengpass().send(session, _order(gattung="stornierung"))
    eintraege = (await session.scalars(select(VersandEngpassEintrag))).all()
    assert len(eintraege) == 1
    assert eintraege[0].gattung == "stornierung"
    assert eintraege[0].betriebsart == "papier"


async def test_zaehler_je_gattung(mit_schluessel, session):
    engpass = Versandengpass()
    await engpass.send(session, _order(gattung="stornierung"))
    await engpass.send(session, _order(gattung="stornierung"))
    await engpass.send(session, _order(gattung="einstieg"))
    assert engpass.zaehler == {"stornierung": 2, "einstieg": 1}
