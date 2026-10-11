import pytest

from app.betriebsart import Betriebsart, BetriebsartFehler, get_betriebsart, ist_live, ist_papier
from app.models.versandengpass import VersandEngpassEintrag


def test_fehlende_betriebsart_verweigert_start(monkeypatch, betriebsart):
    monkeypatch.delenv("BETRIEBSART", raising=False)
    get_betriebsart.cache_clear()
    with pytest.raises(BetriebsartFehler):
        get_betriebsart()


def test_unbekannter_wert_verweigert_start(betriebsart):
    betriebsart("papier_demo")  # Tippfehler-Fall, kein gültiger Wert
    with pytest.raises(BetriebsartFehler):
        get_betriebsart()


def test_gueltiger_wert_wird_einmal_gelesen_und_bleibt_unveraendert(betriebsart, monkeypatch):
    betriebsart("papier")
    assert get_betriebsart() is Betriebsart.PAPIER
    assert ist_papier() and not ist_live()

    # Die Umgebungsvariable ändert sich -- der Prozess liest sie aber nicht erneut.
    monkeypatch.setenv("BETRIEBSART", "live")
    assert get_betriebsart() is Betriebsart.PAPIER


def test_live_wert(betriebsart):
    betriebsart("live")
    assert get_betriebsart() is Betriebsart.LIVE
    assert ist_live() and not ist_papier()


def _eintrag(**overrides) -> VersandEngpassEintrag:
    felder = dict(
        gattung="stornierung",
        strategie_schluessel="s1",
        exchange="okx",
        angenommen=True,
        ablehnungsgrund=None,
        order_referenz={},
        gegenstellen_order_id=None,
    )
    felder.update(overrides)
    return VersandEngpassEintrag(**felder)


async def test_betriebsart_mixin_setzt_prozess_betriebsart_beim_speichern(betriebsart, session):
    betriebsart("papier")
    eintrag = _eintrag()
    session.add(eintrag)
    await session.flush()
    assert eintrag.betriebsart == "papier"


def test_betriebsart_mixin_verweigert_fremden_wert(betriebsart):
    betriebsart("papier")
    eintrag = _eintrag(betriebsart="papier")
    with pytest.raises(BetriebsartFehler):
        eintrag.betriebsart = "live"


def test_betriebsart_mixin_akzeptiert_eigenen_wert(betriebsart):
    betriebsart("live")
    eintrag = _eintrag(betriebsart="live")
    assert eintrag.betriebsart == "live"
