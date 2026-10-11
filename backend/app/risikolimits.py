"""Risikolimits aus Risikopolitik Abschnitt 4 (HED-8 Fassung 1.1), eine einzige
maschinenlesbare Quelle für B-2 Stufe 1 und Stufe 2 (Entwurf HED-36 Abschnitt 6,
Festlegung 6.1.1: "Die Limitliste wird aus einer einzigen Quelle gelesen").

Jede Prüffunktion liefert ein :class:`LimitErgebnis`. ``berechenbar=False`` bedeutet:
die nötigen Daten oder Infrastruktur fehlen in dieser Installation heute -- nicht,
dass das Limit nicht existiert. Festlegung 6.1.3 gilt wörtlich: ein nicht
berechenbares Limit gilt als gebrochen (fail closed). Jeder solche Fall ist unten
einzeln benannt und mit dem fehlenden Baustein kommentiert, nicht pauschal mit einem
einzigen Sammel-Grund.

Dieses Modul enthält **keine** I/O. Aufrufer (``app.trading.stufe1``,
``app.trading.stufe2``) sammeln die Rohwerte (NAV, Historie, offene Positionen,
Korrelation) und reichen sie als :class:`RisikoKontext` herein. Das hält die
Prüflogik selbst testbar ohne Datenbank und Marktdaten.
"""

from dataclasses import dataclass
from decimal import Decimal

HUNDERT = Decimal("100")

# Freigegebene Tranchen, HED-8 Abschnitt 9.1. Welche Tranche gerade gilt, ist eine
# Board-Entscheidung (app.config.Settings.freigegebene_tranche), keine Risikozahl --
# die Eurobeträge hier stehen unverändert so in der freigegebenen Risikopolitik.
TRANCHEN: dict[str, Decimal] = {
    "B1": Decimal("2000"),
    "B2": Decimal("4000"),
    "B3": Decimal("6000"),
}
NAV_STARTKAPITAL = Decimal("10000")  # HED-8 Abschnitt 4: Eurobeträge setzen NAV = 10.000 € an


@dataclass(frozen=True, slots=True)
class LimitErgebnis:
    """Eine Zeile aus Stufe 1 Schritt 1.11 / Stufe 2 Schritt 2.8."""

    limit: str
    berechenbar: bool
    gebrochen: bool
    auslastung_pct: Decimal | None  # None nur wenn nicht berechenbar
    begruendung: str

    def to_json(self) -> dict:
        return {
            "limit": self.limit,
            "berechenbar": self.berechenbar,
            "gebrochen": self.gebrochen,
            "auslastung_pct": (
                str(self.auslastung_pct) if self.auslastung_pct is not None else None
            ),
            "begruendung": self.begruendung,
        }


def _nicht_berechenbar(limit: str, begruendung: str) -> LimitErgebnis:
    """Festlegung 6.1.3: aus der Quelle nicht lesbar/nicht ermittelbar = gebrochen."""
    return LimitErgebnis(limit, berechenbar=False, gebrochen=True, auslastung_pct=None,
                         begruendung=begruendung)


def _schwelle(limit: str, wert: Decimal, schwelle: Decimal, begruendung: str) -> LimitErgebnis:
    """``wert`` gegen eine Obergrenze ``schwelle`` (beide nicht-negativ, z. B. Euro-Beträge
    oder Prozentpunkte). Auslastung über 100 % heißt gebrochen."""
    if schwelle <= 0:
        return _nicht_berechenbar(limit, "Schwelle ist null oder negativ, nicht auswertbar")
    auslastung = (wert / schwelle) * HUNDERT
    return LimitErgebnis(limit, berechenbar=True, gebrochen=auslastung > HUNDERT,
                         auslastung_pct=auslastung, begruendung=begruendung)


def _hoechstens(limit: str, zahl: int, maximum: int, begruendung: str) -> LimitErgebnis:
    auslastung = (Decimal(zahl) / Decimal(maximum)) * HUNDERT if maximum > 0 else Decimal(0)
    return LimitErgebnis(limit, berechenbar=True, gebrochen=zahl > maximum,
                         auslastung_pct=auslastung, begruendung=begruendung)


def tranchen_grenze(tranche: str) -> Decimal | None:
    return TRANCHEN.get(tranche)


def strengerer_wert(nav_anteil: Decimal, nav: Decimal, tranche_anteil: Decimal,
                     tranche_eur: Decimal) -> Decimal:
    """"Es gilt der strengere Wert" (HED-8 Abschnitt 4, Kopfsatz): das Minimum aus
    NAV-bezogener und tranchebezogener Eurogrenze."""
    return min(nav_anteil * nav, tranche_anteil * tranche_eur)


@dataclass(frozen=True, slots=True)
class RisikoKontext:
    """Rohwerte für genau einen Prüflauf (Stufe 1 oder Stufe 2), bereits aus DB und
    Marktdaten gesammelt. ``None`` bei einem Feld heißt: nicht ermittelbar."""

    tranche: str
    tranche_eur: Decimal
    nav: Decimal
    nav_hoechststand: Decimal | None
    nav_tagesanfang: Decimal | None
    nav_vor_7_tagen: Decimal | None
    nav_monatsanfang: Decimal | None
    cash: Decimal
    brutto_exposure: Decimal | None
    anzahl_offener_positionen: int
    einzelwert_marktwert_nach_trade: Decimal | None
    summenrisiko_vor_trade: Decimal | None
    summenrisiko_nach_trade: Decimal | None
    korrelation_zu_offenen_positionen: Decimal | None
    hat_offene_positionen: bool
    risiko_je_trade_eur: Decimal | None
    positionsgroesse_implizierte_obergrenze: Decimal | None
    proposal_size: Decimal


def _risiko_je_trade(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.risiko_je_trade_eur is None:
        return _nicht_berechenbar(
            "risiko_je_trade", "Kurs/FX für das Instrument nicht lesbar, Risiko in EUR unbekannt"
        )
    grenze = strengerer_wert(Decimal("0.005"), ctx.nav, Decimal("0.02"), ctx.tranche_eur)
    return _schwelle(
        "risiko_je_trade", ctx.risiko_je_trade_eur, grenze,
        "min(0,5% NAV; 2% Tranche), HED-8 Tabelle 4.2",
    )


def _positionsgroesse_rechenweg(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.positionsgroesse_implizierte_obergrenze is None:
        return _nicht_berechenbar(
            "positionsgroesse_rechenweg",
            "Stop-Abstand oder Risiko in EUR nicht auswertbar (0, fehlend oder Kurs nicht lesbar)",
        )
    return _schwelle(
        "positionsgroesse_rechenweg", ctx.proposal_size,
        ctx.positionsgroesse_implizierte_obergrenze,
        "Risikobudget / (1,5 x Stop-Abstand in %), HED-8 Tabelle 4.2",
    )


def _einzelwert_obergrenze_krypto(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.einzelwert_marktwert_nach_trade is None:
        return _nicht_berechenbar(
            "einzelwert_obergrenze_krypto", "Kurs/FX für das Instrument nicht lesbar"
        )
    grenze = strengerer_wert(Decimal("0.10"), ctx.nav, Decimal("0.20"), ctx.tranche_eur)
    return _schwelle(
        "einzelwert_obergrenze_krypto", ctx.einzelwert_marktwert_nach_trade, grenze,
        "min(10% NAV; 20% Tranche), HED-8 Tabelle 4.2 (nur Krypto, live bis Gate C)",
    )


def _brutto_exposure(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.brutto_exposure is None:
        return _nicht_berechenbar("brutto_exposure", "Kurs/FX für das Instrument nicht lesbar")
    return _schwelle(
        "brutto_exposure", ctx.brutto_exposure, ctx.tranche_eur,
        "Brutto-Exposure <= freigegebene Tranche, HED-8 Tabelle 4.1",
    )


def _summenrisiko(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.summenrisiko_nach_trade is None:
        return _nicht_berechenbar(
            "summenrisiko",
            "Mindestens eine offene Position ohne auslesbaren börsenseitigen Stop "
            "(B-12 noch nicht gebaut/verdrahtet) -- Summenrisiko ist ohne Stop-Distanz "
            "jeder Position nicht ermittelbar",
        )
    grenze = strengerer_wert(Decimal("0.015"), ctx.nav, Decimal("0.06"), ctx.tranche_eur)
    return _schwelle(
        "summenrisiko", ctx.summenrisiko_nach_trade, grenze,
        "min(1,5% NAV; 6% Tranche) aller offenen Positionen nach Trade, HED-8 Tabelle 4.1",
    )


def _drawdown_vom_hoechststand(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.nav_hoechststand is None or ctx.nav_hoechststand <= 0:
        return _nicht_berechenbar(
            "drawdown_vom_hoechststand", "kein historischer Höchststand (ValueSnapshot) vorhanden"
        )
    drawdown_pct = (ctx.nav_hoechststand - ctx.nav) / ctx.nav_hoechststand * HUNDERT
    return LimitErgebnis(
        "drawdown_vom_hoechststand", berechenbar=True, gebrochen=drawdown_pct >= Decimal(10),
        auslastung_pct=(drawdown_pct / Decimal(10)) * HUNDERT,
        begruendung="-10% vom historischen Höchststand, HED-8 Tabelle 4.1 -> Stufe 2",
    )


def _tagesverlust(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.nav_tagesanfang is None:
        return _nicht_berechenbar("tagesverlust", "kein Tagesanfangs-Snapshot vorhanden")
    verlust = max(Decimal(0), ctx.nav_tagesanfang - ctx.nav)
    grenze = strengerer_wert(Decimal("0.02"), ctx.nav, Decimal("0.05"), ctx.tranche_eur)
    return _schwelle(
        "tagesverlust", verlust, grenze,
        "min(2% NAV; 5% Tranche), HED-8 Tabelle 4.1 -> Handelsstopp bis nächster Handelstag",
    )


def _verlust_7_tage(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.nav_vor_7_tagen is None:
        return _nicht_berechenbar("verlust_7_tage", "kein Snapshot von vor 7 Tagen vorhanden")
    verlust = max(Decimal(0), ctx.nav_vor_7_tagen - ctx.nav)
    grenze = strengerer_wert(Decimal("0.04"), ctx.nav, Decimal("0.10"), ctx.tranche_eur)
    return _schwelle(
        "verlust_7_tage", verlust, grenze,
        "min(4% NAV; 10% Tranche) rollierend, HED-8 Tabelle 4.1 -> Brutto-Exposure halbieren",
    )


def _kalendermonatsverlust(ctx: RisikoKontext) -> LimitErgebnis:
    if ctx.nav_monatsanfang is None:
        return _nicht_berechenbar(
            "kalendermonatsverlust", "kein Snapshot vom 1. Kalendertag vorhanden"
        )
    verlust = max(Decimal(0), ctx.nav_monatsanfang - ctx.nav)
    grenze = strengerer_wert(Decimal("0.06"), ctx.nav, Decimal("0.15"), ctx.tranche_eur)
    return _schwelle(
        "kalendermonatsverlust", verlust, grenze,
        "min(6% NAV; 15% Tranche), HED-8 Tabelle 4.1 -> Stufe 2, Tranche eine Stufe zurück",
    )


def _harte_kapitalgrenze(ctx: RisikoKontext) -> LimitErgebnis:
    kumulierter_verlust = max(Decimal(0), NAV_STARTKAPITAL - ctx.nav)
    nav_grenze_verletzt = ctx.nav <= Decimal(8000)
    verlust_grenze = Decimal("0.35") * ctx.tranche_eur
    verlust_grenze_verletzt = kumulierter_verlust >= verlust_grenze
    gebrochen = nav_grenze_verletzt or verlust_grenze_verletzt
    auslastung = max(
        (Decimal(8000) / ctx.nav * HUNDERT) if ctx.nav > 0 else Decimal(999),
        (kumulierter_verlust / verlust_grenze * HUNDERT) if verlust_grenze > 0 else Decimal(0),
    )
    return LimitErgebnis(
        "harte_kapitalgrenze", berechenbar=True, gebrochen=gebrochen, auslastung_pct=auslastung,
        begruendung="NAV <= 8.000 € oder kumulierter Verlust >= 35% der höchsten Tranche, "
        "HED-8 Tabelle 4.1 -> Stufe 3, alles liquidieren",
    )


def _korrelation_zu_offenen_positionen(ctx: RisikoKontext) -> LimitErgebnis:
    if not ctx.hat_offene_positionen:
        return LimitErgebnis(
            "korrelierte_gruppe", berechenbar=True, gebrochen=False, auslastung_pct=Decimal(0),
            begruendung="keine offenen Positionen, keine Gruppe zu bilden",
        )
    if ctx.korrelation_zu_offenen_positionen is None:
        return _nicht_berechenbar(
            "korrelierte_gruppe",
            "Korrelation erfordert tagesgenaue EUR-Renditen über 60 Tage; diese Installation "
            "hat keine historischen EUR/USD-Tageskurse gespeichert, nur den aktuellen fx()-Kurs "
            "(app.marketdata.service.MarketService.fx) -- eine Annäherung über den heutigen "
            "Kurs würde die in HED-8 Abschnitt 4.3 verlangte EUR-Bezugsgröße verfälschen. "
            "Fehlender Baustein: historische FX-Kurs-Speicherung je Handelstag.",
        )
    gebrochen = abs(ctx.korrelation_zu_offenen_positionen) >= Decimal("0.6")
    return LimitErgebnis(
        "korrelierte_gruppe", berechenbar=True, gebrochen=gebrochen,
        auslastung_pct=abs(ctx.korrelation_zu_offenen_positionen) / Decimal("0.6") * HUNDERT,
        begruendung="|rho| >= 0,6 auf log. Tagesrenditen in EUR über 60 Tage, HED-8 Abschnitt 4.3",
    )


def _offene_positionen_zahl(ctx: RisikoKontext) -> LimitErgebnis:
    grenze = {"B1": 4, "B2": 6, "B3": 8}.get(ctx.tranche)
    if grenze is None:
        return _nicht_berechenbar("offene_positionen_zahl", f"unbekannte Tranche {ctx.tranche!r}")
    return _hoechstens(
        "offene_positionen_zahl", ctx.anzahl_offener_positionen, grenze,
        "<= 4 / 6 / 8 je Tranche B1/B2/B3, HED-8 Tabelle 4.1",
    )


# Limits, für die dieser Installation heute die nötigen Daten oder Infrastruktur
# fehlen -- jeder einzeln benannt, mit dem fehlenden Baustein, nicht pauschal.
# Festlegung 6.1.3: nicht berechenbar = gebrochen, also fail closed, nicht "optimistisch
# als bestanden werten". Jede Zeile hier ist ein Kandidat für ein eigenes Folgeissue.
_NICHT_VERDRAHTETE_LIMITS: tuple[tuple[str, str], ...] = (
    ("mindest_barbestand",
     "Reserve außerhalb der Börse (HED-8 Tabelle 4.1) wird nirgends in der Plattform "
     "erfasst; ohne sie ist der Gesamt-Barbestand nicht vollständig, nur der "
     "Börsenanteil"),
    ("boersenguthaben_ueberschuss",
     "Dieselbe fehlende Reserve-Erfassung macht 'Tranche + 10%' gegen den GESAMTEN "
     "Barbestand nicht prüfbar, nur gegen den Börsenanteil"),
    ("eine_strategie_anteil",
     "Keine Zuordnung Proposal/Order -> Kapitalanteil je Strategie gegenüber anderen "
     "Strategien im selben Portfolio; Strategy-Modell (HED-41) trägt kein Kapital-Soll"),
    ("zweite_strategie_korrelation",
     "Dieselbe fehlende historische EUR-FX-Speicherung wie korrelierte_gruppe"),
    ("neue_strategie_erste_4_wochen",
     "Kein Feld, seit wann eine Strategie im Live-/Papierbetrieb läuft (Start-Zeitstempel "
     "fehlt im Strategy-Modell)"),
    ("usd_gesamtexposure",
     "Keine Auswertung je Position, ob die Quote-Währung USD/USDT ist, aggregiert über "
     "das gesamte Portfolio (nur Einzelposition wird bewertet, kein Währungs-Rollup)"),
    ("usdt_guthaben_abweichung",
     "Keine laufende Prüfung der USDT-Nennwertabweichung gegen einen Referenzkurs"),
    ("steuerruecklage",
     "Keine Verbuchung realisierter Jahresgewinne in eine separate, bar gehaltene "
     "Rückstellung außerhalb Tranche/NAV"),
    ("ein_broker_anteil",
     "Vor Gate C ohnehin 100% OKX per Policy; keine Mehr-Börsen-Infrastruktur, die das "
     "prüfen könnte -- Platzhalter für Gate C"),
    ("liquiditaet_volumen_marktkapitalisierung",
     "Marktkapitalisierung und Erstnotierungsdatum werden nirgends gespeichert "
     "(app.models.assets.Asset hat kein market_cap-/listed_since-Feld); 30-Tage-Median-"
     "Volumen ist über MarketService.history() grundsätzlich berechenbar, aber ohne die "
     "anderen beiden Bedingungen ist das Gesamtlimit nicht vollständig auswertbar"),
    ("spanne_gebot_brief",
     "Quote.bid/ask sind vom Adapter vorgesehen, aber noch nicht in einen "
     "Stufe-1/2-Prüfschritt verdrahtet -- technisch nah, aber nicht Teil dieser PR"),
    ("ausstiegsliquiditaet",
     "Gilt für bereits gehaltene Positionen, nicht für die neue Order selbst, und "
     "braucht einen laufenden Hintergrundprozess, keinen Order-Zeitpunkt-Check"),
    ("zweite_preisquelle_plausibilisierung",
     "Kein zweiter, unabhängig konfigurierter Live-Kursabgleich pro Instrument verdrahtet"),
    ("keine_market_order_fenster",
     "Betrifft nur Market-Orders; B-2 Stufe 2 verwendet ausschließlich Limit-Orders "
     "(Entwurf Abschnitt 6.3) -- hier als Platzhalter geführt, weil die Risikopolitik "
     "ihn als eigenes Limit nennt"),
    ("orderrate",
     "Kein laufender Zähler 'Orders der letzten 24h' je Installation; "
     "VersandEngpassEintrag (HED-42) hat Zeitstempel und könnte das liefern -- eigenes "
     "Folgeissue, kein Stufe-1/2-Berechnungsproblem"),
    ("umschlagskosten_monatlich",
     "Keine laufende Summierung Gebühren + realisierte Slippage je Kalendermonat"),
    ("verlustserie_je_strategie",
     "Kein laufender Zähler aufeinanderfolgender Verlusttrades je Strategie"),
    ("stop_slippage_mittel",
     "SlippageMessungEintrag (HED-49) existiert, aber noch keine Auswertung '> 1,5x "
     "Annahme im Mittel über 10 Trades' je Strategie"),
    ("wochenend_halbe_positionsgroesse",
     "Keine Kalenderprüfung 'Sa/So' mit automatischer Halbierung der Positionsgröße vor "
     "der Risikoprüfung"),
)


def _nicht_verdrahtet_ergebnisse() -> list[LimitErgebnis]:
    return [_nicht_berechenbar(name, grund) for name, grund in _NICHT_VERDRAHTETE_LIMITS]


# Die vollständige Reihenfolge, Festlegung 6.1.1: "läuft über alle Limits aus
# Risikopolitik Abschnitt 4, nicht über eine Auswahl". Jedes Limit aus HED-8
# Abschnitt 4 ist hier namentlich vertreten -- entweder mit echter Berechnung oder
# als benannter, fail-closed Platzhalter oben.
_BERECHNETE_PRUEFUNGEN = (
    _risiko_je_trade,
    _positionsgroesse_rechenweg,
    _einzelwert_obergrenze_krypto,
    _brutto_exposure,
    _summenrisiko,
    _drawdown_vom_hoechststand,
    _tagesverlust,
    _verlust_7_tage,
    _kalendermonatsverlust,
    _harte_kapitalgrenze,
    _korrelation_zu_offenen_positionen,
    _offene_positionen_zahl,
)


def pruefe_alle_limits(ctx: RisikoKontext) -> list[LimitErgebnis]:
    """Schritt 1.11 / Schritt 2.8: jedes Limit einzeln, in fester Reihenfolge.

    Wird von Stufe 1 und Stufe 2 mit demselben Kontext-Typ aufgerufen (Festlegung
    6.1.1, einzige Quelle) -- Stufe 2 übergibt einen frisch neu berechneten Kontext,
    nicht den eingefrorenen aus Stufe 1.
    """
    return [p(ctx) for p in _BERECHNETE_PRUEFUNGEN] + _nicht_verdrahtet_ergebnisse()
