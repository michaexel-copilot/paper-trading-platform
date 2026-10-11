"""Preisband-Formel, B-2 Stufe 2 (Entwurf HED-36 Abschnitt 6.3, CRO-freigegeben über
HED-33 Fassung 1.1 final). Reine Funktionen, keine I/O.

Zwei Bedingungen, beide müssen gelten (6.3):
(i)  Banduntergrenze <= aktueller Kurs <= Bandobergrenze
(ii) Long: aktueller Kurs > Stop-Kurs; Short: aktueller Kurs < Stop-Kurs

Gewählte Umsetzung: Limit-Order zum Grenzpreis, nicht Market-Order mit Nachprüfung
(Begründung in Abschnitt 6.3). Der Grenzpreis wird am Ende von Stufe 1 einmalig
berechnet und auf ``Proposal.limit_price`` eingefroren (Festlegung 6.1.4: Kennzahlen
zum Stufe-1-Zeitpunkt), damit das Dashboard "Freigeben bis maximal Grenzpreis X"
zeigen kann, bevor das Board entscheidet.
"""

from decimal import Decimal

ROHE_BANDBREITE_FAKTOR = Decimal("0.20")  # 20% des Stop-Abstands
BANDBREITE_MINIMUM = Decimal("0.3")  # Prozentpunkte
BANDBREITE_MAXIMUM = Decimal("3.0")  # Prozentpunkte


def wirksame_bandbreite_pct(stop_distance_fraction: Decimal) -> Decimal:
    """Wirksame Bandbreite in Prozentpunkten.

    ``stop_distance_fraction`` ist die Kennzahl, wie sie auf ``Proposal`` liegt
    (Bruch, z. B. 0,01 für 1 %, siehe ``app.trading.proposals.create_proposal``).
    Die Formel in Abschnitt 6.3 rechnet in Prozentpunkten (1,0 bedeutet 1 %), daher
    die Umrechnung ``* 100`` hier und nur hier.
    """
    stop_distance_pct_punkte = stop_distance_fraction * 100
    rohe_breite = ROHE_BANDBREITE_FAKTOR * stop_distance_pct_punkte
    return max(BANDBREITE_MINIMUM, min(BANDBREITE_MAXIMUM, rohe_breite))


def band_grenzen(
    proposal_price: Decimal, stop_distance_fraction: Decimal
) -> tuple[Decimal, Decimal]:
    """(Banduntergrenze, Bandobergrenze), symmetrisch um ``proposal_price``."""
    breite = wirksame_bandbreite_pct(stop_distance_fraction) / Decimal(100)
    return proposal_price * (Decimal(1) - breite), proposal_price * (Decimal(1) + breite)


def innerhalb_des_bandes(
    kurs: Decimal, proposal_price: Decimal, stop_distance_fraction: Decimal
) -> bool:
    unten, oben = band_grenzen(proposal_price, stop_distance_fraction)
    return unten <= kurs <= oben


def jenseits_des_stops(kurs: Decimal, stop_price: Decimal, direction: str) -> bool:
    if direction == "long":
        return kurs > stop_price
    return kurs < stop_price  # short


def berechne_grenzpreis(
    direction: str, proposal_price: Decimal, stop_price: Decimal, stop_distance_fraction: Decimal
) -> Decimal:
    """Grenzpreis für die Limit-Order, Abschnitt 6.3 "Gewählte Umsetzung".

    Long: min(Bandobergrenze, ...); Short: max(Banduntergrenze, ...) -- "weitere
    Preisobergrenzen/-untergrenzen" aus dem Entwurf (z. B. Slippage-Kappung) sind
    hier nicht Teil dieser PR, siehe HED-44 PR-Beschreibung. Bedingung (ii) wird vom
    Aufrufer zusätzlich und unabhängig geprüft (Entwurf: "Die Limit-Order allein kann
    Bedingung (ii) nicht erzwingen").
    """
    unten, oben = band_grenzen(proposal_price, stop_distance_fraction)
    return oben if direction == "long" else unten
