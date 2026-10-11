"""Risikolimits aus Risikopolitik Abschnitt 4 (HED-8), die einzige maschinenlesbare
Quelle für B-2 Stufe 1/2 (Entwurf HED-36 Abschnitt 6, Festlegung 6.1.1/6.1.3).

Reine Tests ohne Datenbank/Marktdaten -- ``RisikoKontext`` wird direkt gebaut.
"""

from decimal import Decimal

from app.risikolimits import RisikoKontext, pruefe_alle_limits, strengerer_wert


def D(value) -> Decimal:
    return Decimal(str(value))


def _basiskontext(**overrides) -> RisikoKontext:
    felder = dict(
        tranche="B1",
        tranche_eur=D(2000),
        nav=D(10000),
        nav_hoechststand=D(10000),
        nav_tagesanfang=D(10000),
        nav_vor_7_tagen=D(10000),
        nav_monatsanfang=D(10000),
        cash=D(9000),
        brutto_exposure=D(300),
        anzahl_offener_positionen=1,
        einzelwert_marktwert_nach_trade=D(300),
        summenrisiko_vor_trade=D(0),
        summenrisiko_nach_trade=D(40),
        korrelation_zu_offenen_positionen=D(0),
        hat_offene_positionen=False,
        risiko_je_trade_eur=D(40),
        positionsgroesse_implizierte_obergrenze=D(1000),
        proposal_size=D("0.01"),
    )
    felder.update(overrides)
    return RisikoKontext(**felder)


def _ergebnis(ergebnisse, limit: str):
    treffer = [e for e in ergebnisse if e.limit == limit]
    assert len(treffer) == 1, f"{limit} nicht genau einmal im Katalog"
    return treffer[0]


# --- Festlegung 6.1.3: jedes nicht verdrahtete Limit ist benannt, fail closed -------------


def test_vollstaendig_konformer_kontext_bricht_trotzdem_am_ersten_nicht_verdrahteten_limit():
    """Dokumentiert ehrlich den Stand dieser PR: kein einziger Vorschlag besteht Stufe 1
    vollständig, bis die in app.risikolimits benannten Folgeissues (Reserve außerhalb
    der Börse, Orderrate, ...) gebaut sind. Das ist Festlegung 6.1.3 (fail closed),
    keine Lücke dieser Prüfung."""
    ergebnisse = pruefe_alle_limits(_basiskontext())
    gebrochene = [e.limit for e in ergebnisse if e.gebrochen]
    berechnete_namen = {
        "risiko_je_trade", "positionsgroesse_rechenweg", "einzelwert_obergrenze_krypto",
        "brutto_exposure", "summenrisiko", "drawdown_vom_hoechststand", "tagesverlust",
        "verlust_7_tage", "kalendermonatsverlust", "harte_kapitalgrenze",
        "korrelierte_gruppe", "offene_positionen_zahl",
    }
    for name in berechnete_namen:
        ergebnis = _ergebnis(ergebnisse, name)
        assert ergebnis.gebrochen is False, f"{name} sollte in diesem Kontext bestehen"
    # Mindestens ein nicht verdrahtetes Limit ist vorhanden und gebrochen.
    assert "mindest_barbestand" in gebrochene
    nicht_berechenbare = [e for e in ergebnisse if not e.berechenbar]
    assert len(nicht_berechenbare) >= 15  # vollständige Liste in app.risikolimits


def test_jedes_limit_kommt_genau_einmal_vor():
    ergebnisse = pruefe_alle_limits(_basiskontext())
    namen = [e.limit for e in ergebnisse]
    assert len(namen) == len(set(namen))


# --- strengerer_wert ("es gilt der strengere Wert") ---------------------------------------


def test_strengerer_wert_nimmt_das_minimum():
    # B1: min(2% von 10.000=200, 5% von 2.000=100) -> 100, HED-8 Tabelle 4.1 Tagesverlust
    grenze = strengerer_wert(D("0.02"), D(10000), D("0.05"), D(2000))
    assert grenze == D(100)


# --- Einzelne Prüfungen: Bruch erkannt -----------------------------------------------------


def test_risiko_je_trade_ueber_grenze_ist_gebrochen():
    ctx = _basiskontext(risiko_je_trade_eur=D(41))  # Grenze B1 = min(50, 40) = 40
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "risiko_je_trade")
    assert ergebnis.gebrochen is True
    assert ergebnis.auslastung_pct > 100


def test_risiko_je_trade_nicht_berechenbar_ohne_fx():
    ctx = _basiskontext(risiko_je_trade_eur=None)
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "risiko_je_trade")
    assert ergebnis.berechenbar is False
    assert ergebnis.gebrochen is True  # Festlegung 6.1.3


def test_summenrisiko_nicht_berechenbar_ohne_stop_auf_bestandsposition():
    ctx = _basiskontext(summenrisiko_nach_trade=None)
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "summenrisiko")
    assert ergebnis.berechenbar is False
    assert ergebnis.gebrochen is True


def test_drawdown_vom_hoechststand_zehn_prozent_ist_gebrochen():
    ctx = _basiskontext(nav=D(9000), nav_hoechststand=D(10000))  # -10%
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "drawdown_vom_hoechststand")
    assert ergebnis.gebrochen is True


def test_drawdown_unter_zehn_prozent_besteht():
    ctx = _basiskontext(nav=D(9500), nav_hoechststand=D(10000))  # -5%
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "drawdown_vom_hoechststand")
    assert ergebnis.gebrochen is False


def test_harte_kapitalgrenze_nav_unter_8000_ist_gebrochen():
    ctx = _basiskontext(nav=D(7999))
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "harte_kapitalgrenze")
    assert ergebnis.gebrochen is True


def test_harte_kapitalgrenze_kumulierter_verlust_35_prozent_tranche_ist_gebrochen():
    # NAV-Startkapital 10.000, Tranche B3 = 6.000 -> 35% = 2.100; NAV = 10.000-2.100 = 7.900
    # liegt zufällig auch unter 8.000 -- stattdessen B1-Tranche (2.000, 35% = 700),
    # NAV = 10.000 - 700 = 9.300, über 8.000, damit nur die Verlustgrenze greift.
    ctx = _basiskontext(nav=D(9300), tranche="B1", tranche_eur=D(2000))
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "harte_kapitalgrenze")
    assert ergebnis.gebrochen is True


def test_korrelation_ohne_offene_positionen_ist_trivial_unbroken():
    ctx = _basiskontext(hat_offene_positionen=False, korrelation_zu_offenen_positionen=D(0))
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "korrelierte_gruppe")
    assert ergebnis.berechenbar is True
    assert ergebnis.gebrochen is False


def test_korrelation_mit_offenen_positionen_ohne_historische_eur_fx_ist_nicht_berechenbar():
    """Abschnitt 12.6: 'eindeutige Definition liegt vor' (HED-8 4.3), aber die
    Installation hat keine historischen EUR/USD-Tageskurse -- siehe Begründung in
    app.risikolimits._korrelation_zu_offenen_positionen."""
    ctx = _basiskontext(hat_offene_positionen=True, korrelation_zu_offenen_positionen=None)
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "korrelierte_gruppe")
    assert ergebnis.berechenbar is False
    assert ergebnis.gebrochen is True


def test_offene_positionen_zahl_ueber_b1_grenze_vier_ist_gebrochen():
    ctx = _basiskontext(tranche="B1", anzahl_offener_positionen=5)
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "offene_positionen_zahl")
    assert ergebnis.gebrochen is True


def test_offene_positionen_zahl_an_b1_grenze_vier_besteht():
    ctx = _basiskontext(tranche="B1", anzahl_offener_positionen=4)
    ergebnis = _ergebnis(pruefe_alle_limits(ctx), "offene_positionen_zahl")
    assert ergebnis.gebrochen is False
