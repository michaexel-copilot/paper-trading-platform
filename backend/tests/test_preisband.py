"""Preisband-Formel, B-2 Stufe 2 (Entwurf HED-36 Abschnitt 6.3).

Deckt die Beispielrechnung aus dem Entwurf (Tabelle "Beispielrechnungen zur
Verdeutlichung der Grenzen") und die beiden Bedingungen (i)/(ii) ab.
"""

from decimal import Decimal

from app.preisband import (
    band_grenzen,
    berechne_grenzpreis,
    innerhalb_des_bandes,
    jenseits_des_stops,
    wirksame_bandbreite_pct,
)


def D(value) -> Decimal:
    return Decimal(str(value))


# --- wirksame_bandbreite_pct: die Beispielzeilen aus Abschnitt 6.3 -----------------------


def test_untergrenze_greift_unter_1_5_prozentpunkte_stop_abstand():
    assert wirksame_bandbreite_pct(D("0.010")) == D("0.3")  # 1,0 Pp Stop-Abstand


def test_genau_an_der_untergrenze_bei_1_5_prozentpunkte():
    assert wirksame_bandbreite_pct(D("0.015")) == D("0.3")


def test_keine_grenze_wirkt_bei_5_prozentpunkte():
    assert wirksame_bandbreite_pct(D("0.05")) == D("1.0")


def test_genau_an_der_obergrenze_bei_15_prozentpunkte():
    assert wirksame_bandbreite_pct(D("0.15")) == D("3.0")


def test_obergrenze_greift_ueber_15_prozentpunkte():
    assert wirksame_bandbreite_pct(D("0.25")) == D("3.0")


# --- band_grenzen -------------------------------------------------------------------------


def test_band_ist_symmetrisch_um_vorschlagskurs():
    unten, oben = band_grenzen(D(100), D("0.05"))  # 5 Pp Stop-Abstand -> Breite 1,0
    assert unten == D("99.0")
    assert oben == D("101.0")


# --- Bedingung (i) / (ii) ------------------------------------------------------------------


def test_bedingung_i_kurs_ausserhalb_band_scheitert():
    assert innerhalb_des_bandes(D("101.5"), D(100), D("0.05")) is False
    assert innerhalb_des_bandes(D("100.5"), D(100), D("0.05")) is True


def test_bedingung_ii_long_muss_ueber_stop_bleiben():
    assert jenseits_des_stops(D("96"), D("95"), "long") is True
    assert jenseits_des_stops(D("94"), D("95"), "long") is False


def test_bedingung_ii_short_muss_unter_stop_bleiben():
    assert jenseits_des_stops(D("94"), D("95"), "short") is True
    assert jenseits_des_stops(D("96"), D("95"), "short") is False


def test_bedingung_ii_schneidet_band_ab_bei_sehr_engem_stop():
    """Entwurf 6.3: Stop-Abstand 0,2 Pp -> wirksame Breite 0,3 (Untergrenze), Band
    reicht dann über den Stop hinaus. Ohne Bedingung (ii) könnte eine Long-Order
    unterhalb ihres eigenen Stops noch als 'im Band' gelten."""
    proposal_price, stop_price = D(100), D("99.85")  # Stop-Abstand 0,15 Pp < 0,2
    unten, _oben = band_grenzen(proposal_price, D("0.0015"))
    assert unten < stop_price  # das Band reicht unter den Stop
    assert innerhalb_des_bandes(D("99.8"), proposal_price, D("0.0015")) is True
    assert jenseits_des_stops(D("99.8"), stop_price, "long") is False


# --- berechne_grenzpreis --------------------------------------------------------------------


def test_grenzpreis_long_ist_bandobergrenze():
    grenzpreis = berechne_grenzpreis("long", D(100), D(95), D("0.05"))
    assert grenzpreis == D("101.0")


def test_grenzpreis_short_ist_banduntergrenze():
    grenzpreis = berechne_grenzpreis("short", D(100), D(105), D("0.05"))
    assert grenzpreis == D("99.0")
