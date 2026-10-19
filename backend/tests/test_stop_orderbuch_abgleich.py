"""Täglicher Stop-Orderbuch-Abgleich (B-12, HED-84, Entwurf Abschnitt 10). Prüfplan
P-7, P-8.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.dbtypes import utcnow
from app.models.kill_switch import SperrkennzeichenProtokoll
from app.models.sperrkennzeichen import FirmenweitesSperrkennzeichen
from app.models.stop_order import StopAbgleichLauf, StopOrderBuch
from app.sperrkennzeichen import ist_firmenweit_gesperrt
from app.trading.papier.okx_demo import OffeneAlgoOrder
from app.trading.stop_orderbuch_abgleich import pruefe_einmal
from app.trading.versandengpass import Versandengpass

TOLERANZ = Decimal("0.001")
TOTMANNFRIST = timedelta(hours=26)


@pytest.fixture(autouse=True)
async def offenes_firmenweites_kennzeichen(session):
    session.add(FirmenweitesSperrkennzeichen(gesetzt=False))
    await session.commit()


@pytest.fixture(autouse=True)
def papier_betriebsart(betriebsart):
    betriebsart("papier")


class _FakeEngpass(Versandengpass):
    def __init__(self, gegenstellen_antwort: dict[str, list[OffeneAlgoOrder] | None]):
        super().__init__()
        self._antwort = gegenstellen_antwort

    async def lese_offene_stop_orders(self, exchange, symbol):
        if symbol not in self._antwort:
            return []
        return self._antwort[symbol]


class _FehlerEngpass(Versandengpass):
    async def lese_offene_stop_orders(self, exchange, symbol):
        return None  # Entwurf 12.1: None bei Fehlschlag


def _algo(algo_id, symbol="BTC/USDC", side="sell", menge="0.5", trigger="47500") -> OffeneAlgoOrder:
    return OffeneAlgoOrder(
        quelle="okx_demo", algo_id=algo_id, symbol=symbol, side=side,
        menge=Decimal(menge), trigger_kurs=Decimal(trigger), zeitstempel=utcnow(),
    )


async def _eigene_zeile(session, **overrides) -> StopOrderBuch:
    felder = dict(
        strategie_schluessel="1", exchange="okx", symbol="BTC/USDC", side="sell",
        menge_soll=Decimal("0.5"), menge_bestaetigt=Decimal("0.5"),
        trigger_soll=Decimal("47500"), trigger_bestaetigt=Decimal("47500"),
        gegenstellen_algo_id="algo-1", zustand="offen",
    )
    felder.update(overrides)
    zeile = StopOrderBuch(**felder)
    session.add(zeile)
    await session.flush()
    return zeile


# --- P-7: fehlend / Waise / Mengenabweichung / Triggerabweichung --------------------


async def test_leer_gegen_leer_ist_bestanden(session, session_factory):
    engpass = _FakeEngpass({})
    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )
    assert bestanden is True
    assert await ist_firmenweit_gesperrt(session) is False
    lauf = await session.scalar(select(StopAbgleichLauf))
    assert lauf.ergebnis == "bestanden"


async def test_uebereinstimmung_ist_bestanden(session, session_factory):
    await _eigene_zeile(session)
    await session.commit()
    engpass = _FakeEngpass({"BTC/USDC": [_algo("algo-1")]})

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is True
    assert await ist_firmenweit_gesperrt(session) is False


async def test_fehlende_eigene_order_setzt_firmenweites_kennzeichen(session, session_factory):
    await _eigene_zeile(session)
    await session.commit()
    engpass = _FakeEngpass({"BTC/USDC": []})  # OKX hat den Stop nicht (mehr)

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is False
    assert await ist_firmenweit_gesperrt(session) is True
    lauf = await session.scalar(select(StopAbgleichLauf))
    assert lauf.ergebnis == "abweichung"
    assert lauf.anzahl_fehlend == 1
    zeilen = list(
        await session.scalars(
            select(SperrkennzeichenProtokoll).where(
                SperrkennzeichenProtokoll.ausloeser == "stop_orderbuch_abweichung"
            )
        )
    )
    assert len(zeilen) == 1


async def test_waise_setzt_firmenweites_kennzeichen(session, session_factory):
    # Keine eigene Zeile, aber OKX hat trotzdem eine Order für BTC/USDC -- die
    # 30-Tage-Rückblick-Erweiterung (10.4) braucht mindestens eine jüngere eigene
    # Historie, um das Symbol überhaupt abzufragen; hier simuliert über eine bereits
    # stornierte Zeile zum selben Symbol.
    await _eigene_zeile(session, zustand="storniert", gegenstellen_algo_id="algo-alt")
    await session.commit()
    engpass = _FakeEngpass({"BTC/USDC": [_algo("algo-waise")]})

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is False
    lauf = await session.scalar(select(StopAbgleichLauf))
    assert lauf.anzahl_waisen == 1


async def test_mengenabweichung_ueber_toleranz_setzt_kennzeichen(session, session_factory):
    await _eigene_zeile(session, menge_bestaetigt=Decimal("0.5"))
    await session.commit()
    # 1% Abweichung, deutlich über der 0,1%-Toleranz.
    engpass = _FakeEngpass({"BTC/USDC": [_algo("algo-1", menge="0.495")]})

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is False
    lauf = await session.scalar(select(StopAbgleichLauf))
    assert lauf.anzahl_mengenabweichung == 1


async def test_mengenabweichung_innerhalb_toleranz_ist_bestanden(session, session_factory):
    await _eigene_zeile(session, menge_bestaetigt=Decimal("0.5"))
    await session.commit()
    # 0,02% Abweichung -- innerhalb der 0,1%-Toleranz.
    engpass = _FakeEngpass({"BTC/USDC": [_algo("algo-1", menge="0.4999")]})

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is True


async def test_triggerabweichung_setzt_kennzeichen(session, session_factory):
    await _eigene_zeile(session, trigger_bestaetigt=Decimal("47500"))
    await session.commit()
    engpass = _FakeEngpass({"BTC/USDC": [_algo("algo-1", trigger="48000")]})

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is False
    lauf = await session.scalar(select(StopAbgleichLauf))
    assert lauf.anzahl_triggerabweichung == 1


async def test_nachfuehrung_laeuft_ist_kein_waisen_alarm(session, session_factory):
    """Entwurf 10.3, Punkt 6: während der laufenden Nachführung ist sowohl der alte
    als auch der neue Stop ein gültiger Zustand."""
    await _eigene_zeile(session, zustand="nachfuehrung_laeuft", gegenstellen_algo_id="algo-alt")
    await _eigene_zeile(session, gegenstellen_algo_id="algo-neu")
    await session.commit()
    engpass = _FakeEngpass(
        {"BTC/USDC": [_algo("algo-alt"), _algo("algo-neu")]}
    )

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is True


# --- P-8: Totmannschalter -----------------------------------------------------------


async def test_fehlgeschlagener_durchlauf_setzt_kennzeichen(session, session_factory):
    engpass = _FehlerEngpass()
    await _eigene_zeile(session)
    await session.commit()

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST
    )

    assert bestanden is False
    assert await ist_firmenweit_gesperrt(session) is True
    lauf = await session.scalar(select(StopAbgleichLauf))
    assert lauf.ergebnis == "fehlgeschlagen"
    zeilen = list(
        await session.scalars(
            select(SperrkennzeichenProtokoll).where(
                SperrkennzeichenProtokoll.ausloeser == "stop_orderbuch_abgleich_fehlgeschlagen"
            )
        )
    )
    assert len(zeilen) == 1


async def test_kein_bestandener_lauf_je_setzt_totmannschalter(session, session_factory):
    from app.trading.stop_orderbuch_abgleich import _pruefe_totmannschalter

    await _pruefe_totmannschalter(session_factory, TOTMANNFRIST)

    assert await ist_firmenweit_gesperrt(session) is True


async def test_juengerer_bestandener_lauf_haelt_totmannschalter_ruhig(session, session_factory):
    from app.trading.stop_orderbuch_abgleich import _pruefe_totmannschalter

    session.add(StopAbgleichLauf(beendet_at=utcnow(), ergebnis="bestanden"))
    await session.commit()

    await _pruefe_totmannschalter(session_factory, TOTMANNFRIST)

    assert await ist_firmenweit_gesperrt(session) is False


async def test_zu_alter_bestandener_lauf_loest_totmannschalter_aus(session, session_factory):
    from app.trading.stop_orderbuch_abgleich import _pruefe_totmannschalter

    zu_alt = utcnow() - TOTMANNFRIST - timedelta(seconds=1)
    session.add(StopAbgleichLauf(beendet_at=zu_alt, ergebnis="bestanden"))
    await session.commit()

    ausgeloest = await _pruefe_totmannschalter(session_factory, TOTMANNFRIST)

    assert ausgeloest is True
    assert await ist_firmenweit_gesperrt(session) is True


# --- Pfad 2 (Entwurf 9.6, CRO-Entscheidung HED-191 Abschnitt 1b): Storno-Sweep wird
# unmittelbar nach jedem Setzen des Kennzeichens ausgelöst ------------------------


async def test_abweichung_loest_storno_sweep_aus(session, session_factory, monkeypatch):
    """Eine Abweichung erzeugt eine Lauf-Zeile mit ``ergebnis="abweichung"``, keine
    mit ``"bestanden"`` -- ohne einen älteren bestandenen Lauf löst derselbe Aufruf
    deshalb zusätzlich den Totmannschalter aus (E-9). Beide Zweige rufen den Sweep mit
    denselben durchgereichten ``engpass``/``market``/``tranche`` auf -- zwei Aufrufe,
    keine stille Dopplung der eigentlichen Sweep-Logik selbst (die ist in
    ``test_storno_sweep.py`` geprüft)."""
    import app.trading.storno_sweep as storno_sweep

    aufrufe = []

    async def fake_sweep(session_factory_arg, engpass_arg, *, market=None, tranche="B1"):
        aufrufe.append((engpass_arg, market, tranche))
        return 0

    monkeypatch.setattr(storno_sweep, "sweep_ruhende_einstiegsorders", fake_sweep)

    await _eigene_zeile(session, zustand="storniert", gegenstellen_algo_id="algo-alt")
    await session.commit()
    engpass = _FakeEngpass({"BTC/USDC": [_algo("algo-waise")]})
    markt_dummy = object()

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST,
        market=markt_dummy, tranche="B2",
    )

    assert bestanden is False
    assert aufrufe == [(engpass, markt_dummy, "B2"), (engpass, markt_dummy, "B2")]


async def test_bestandener_abgleich_loest_keinen_storno_sweep_aus(
    session, session_factory, monkeypatch
):
    import app.trading.storno_sweep as storno_sweep

    aufrufe = []

    async def fake_sweep(session_factory_arg, engpass_arg, *, market=None, tranche="B1"):
        aufrufe.append((engpass_arg, market, tranche))
        return 0

    monkeypatch.setattr(storno_sweep, "sweep_ruhende_einstiegsorders", fake_sweep)

    engpass = _FakeEngpass({})

    bestanden = await pruefe_einmal(
        session_factory, engpass, mengen_rundungstoleranz=TOLERANZ, totmannfrist=TOTMANNFRIST,
    )

    assert bestanden is True
    assert aufrufe == []


async def test_bestandener_lauf_verhindert_totmannschalter_und_sweep(
    session, session_factory, monkeypatch
):
    """Gegenprobe zu oben: ein frischer bestandener Lauf (z. B. aus demselben
    ``pruefe_einmal``-Aufruf) hält den Totmannschalter ruhig -- ``_pruefe_
    totmannschalter`` meldet das über den Rückgabewert, der Aufrufer löst dann
    keinen Sweep aus."""
    import app.trading.storno_sweep as storno_sweep
    from app.trading.stop_orderbuch_abgleich import _pruefe_totmannschalter

    aufrufe = []

    async def fake_sweep(session_factory_arg, engpass_arg, *, market=None, tranche="B1"):
        aufrufe.append((engpass_arg, market, tranche))
        return 0

    monkeypatch.setattr(storno_sweep, "sweep_ruhende_einstiegsorders", fake_sweep)

    session.add(StopAbgleichLauf(beendet_at=utcnow(), ergebnis="bestanden"))
    await session.commit()

    ausgeloest = await _pruefe_totmannschalter(session_factory, TOTMANNFRIST)

    assert ausgeloest is False
    assert aufrufe == []
