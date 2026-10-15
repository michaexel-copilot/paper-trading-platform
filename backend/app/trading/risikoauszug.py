"""B-11/HED-83 -- maschinenlesbarer Tages-Risikoauszug (Risikopolitik HED-8 Fassung
1.4 Abschnitt 3.2, Tabelle "Bedingungen für Gate B"; Entwurf nicht erforderlich,
Hausordnung Abschnitt "Dein Teil daraus" verlangt das nur für B-2/B-3/B-12/
Ausführungsschicht).

Liefert einen Schnappschuss des eigenen Buchs (keine Börsen-/Gegenstellen-Daten,
siehe HED-83 "Abhängigkeiten"): NAV, Tages-P&L (aufgeteilt in Handelsergebnis und
Währungseffekt, siehe :func:`_pnl_aufteilung`), Drawdown, Brutto-Exposure,
Summenrisiko, Barquote/-bestand, USD-/USDT-/USDC-Anteil, Zahl offener Positionen,
Positionen ohne börsenseitigen Stop und die Ausnutzung jedes Buch-Limits aus
``app.risikolimits`` in Prozent.

Wiederverwendet bewusst dieselben Hilfsfunktionen wie B-2 Stufe 1
(``app.trading.stufe1``: Snapshot-Historie, offene Stop-Preise, Summenrisiko) --
"gleiche Quelle, gleiche Messweise" (Entwurf-Konvention dieses Repos), keine zweite
Implementierung derselben Buchzahlen.

Nicht Gegenstand dieses Moduls (Abgrenzung):
- Die Vier-Tabelle-4.2-Limits, die nur zum Orderzeitpunkt für einen konkreten
  Vorschlag Sinn ergeben (``_NUR_ORDERZEITPUNKT`` unten) -- die laufen über
  ``app.trading.stufe1``/``stufe2`` und werden dort je Proposal protokolliert, nicht
  hier noch einmal für einen nicht existierenden Vorschlag simuliert.
- ``boersenguthaben_ueberschuss`` -- weiterhin in
  ``app.risikolimits._NICHT_VERDRAHTETE_LIMITS`` benannt (Formel nicht zweifelsfrei
  abgeleitet), erscheint hier also als nicht berechenbar, wie jedes andere noch
  nicht verdrahtete Limit auch.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.marketdata.service import MarketService
from app.models import Portfolio
from app.risikolimits import LimitErgebnis, RisikoKontext, pruefe_alle_limits
from app.trading.stufe1 import (
    hoechststand,
    letzter_snapshot_vor,
    letzter_wert_vor,
    offene_stop_preise,
    summenrisiko_offener_positionen,
)
from app.trading.valuation import Valuation, value_portfolio

# HED-8 Tabelle 4.2 + Abschnitt 4.5: Limits, die nur im Zusammenhang mit einem
# konkreten, zum Versand anstehenden Vorschlag eine Aussage treffen -- ohne Trade
# wäre "gebrochen"/"bestanden" für sie eine erfundene Zahl, kein Messwert. Werden
# aus dem Tagesauszug herausgefiltert (nicht neu bewertet), nicht als "bestanden"
# vorgetäuscht.
_NUR_ORDERZEITPUNKT = frozenset({
    "risiko_je_trade", "positionsgroesse_rechenweg", "einzelwert_obergrenze_krypto",
    "short_verbot", "nachkauf_in_verlust",
})


@dataclass(frozen=True, slots=True)
class PnlAufteilung:
    """Tages-P&L, aufgeteilt in Handelsergebnis und Währungseffekt (HED-83).

    Methode (dokumentierte Vereinfachung, keine stille Annahme): Währungseffekt ist
    die Summe über alle seit Tagesanfang weiter gehaltenen Positionen von
    ``menge_heute * kurs_heute_in_quotewaehrung * (fx_heute - fx_tagesanfang)`` --
    die FX-Neubewertung des heutigen Bestands zum heutigen Kurs. Handelsergebnis ist
    der GESAMTE Rest (Tages-P&L minus Währungseffekt): echte Handelsergebnisse,
    Gebühren, realisierte Trades, neu eröffnete/geschlossene Positionen und der
    Kreuzterm aus gleichzeitiger Kurs- und FX-Bewegung. Diese Konstruktion
    garantiert exakt ``handelsergebnis + waehrungseffekt == tages_pnl_gesamt`` --
    kein Rundungsrest, keine Doppelzählung.
    """

    berechenbar: bool
    grund: str | None
    tages_pnl_gesamt: Decimal | None
    handelsergebnis: Decimal | None
    waehrungseffekt: Decimal | None


@dataclass(frozen=True, slots=True)
class Risikoauszug:
    generated_at: datetime
    portfolio_id: int

    nav: Decimal
    drawdown: Decimal | None
    drawdown_pct: Decimal | None

    pnl: PnlAufteilung

    brutto_exposure: Decimal
    summenrisiko: Decimal | None

    cash_boerse: Decimal
    cash_ausserhalb_boerse: Decimal
    cash_gesamt: Decimal
    barquote_pct: Decimal

    anteil_je_quote_waehrung_pct: dict[str, Decimal]
    usd_anteil_pct: Decimal
    usdt_anteil_pct: Decimal
    usdc_anteil_pct: Decimal

    anzahl_offener_positionen: int
    positionen_ohne_stop: list[str]

    limits: list[LimitErgebnis]

    def to_json(self) -> dict:
        def dec(value):
            return str(value) if value is not None else None

        return {
            "generated_at": self.generated_at.isoformat(),
            "portfolio_id": self.portfolio_id,
            "nav": dec(self.nav),
            "drawdown": dec(self.drawdown),
            "drawdown_pct": dec(self.drawdown_pct),
            "tages_pnl_gesamt": dec(self.pnl.tages_pnl_gesamt),
            "tages_pnl_handelsergebnis": dec(self.pnl.handelsergebnis),
            "tages_pnl_waehrungseffekt": dec(self.pnl.waehrungseffekt),
            "tages_pnl_berechenbar": self.pnl.berechenbar,
            "tages_pnl_grund": self.pnl.grund,
            "brutto_exposure": dec(self.brutto_exposure),
            "summenrisiko": dec(self.summenrisiko),
            "cash_boerse": dec(self.cash_boerse),
            "cash_ausserhalb_boerse": dec(self.cash_ausserhalb_boerse),
            "cash_gesamt": dec(self.cash_gesamt),
            "barquote_pct": dec(self.barquote_pct),
            "anteil_je_quote_waehrung_pct": {
                k: dec(v) for k, v in self.anteil_je_quote_waehrung_pct.items()
            },
            "usd_anteil_pct": dec(self.usd_anteil_pct),
            "usdt_anteil_pct": dec(self.usdt_anteil_pct),
            "usdc_anteil_pct": dec(self.usdc_anteil_pct),
            "anzahl_offener_positionen": self.anzahl_offener_positionen,
            "positionen_ohne_stop": self.positionen_ohne_stop,
            "limits": [e.to_json() for e in self.limits],
        }


async def _sammle_buchkontext(
    session: AsyncSession, market: MarketService, portfolio: Portfolio, valuation: Valuation,
    *, tranche: str, tranche_eur: Decimal,
) -> RisikoKontext:
    """Wie ``app.trading.stufe1.sammle_kontext``, aber ohne einen konkreten
    Vorschlag -- der heutige Buchbestand selbst ist der zu prüfende Zustand, keine
    hypothetische Order. Trade-spezifische Felder bekommen neutrale Werte (0 /
    None), die Felder, die diese auswerten (``_NUR_ORDERZEITPUNKT``), werden vom
    Aufrufer (:func:`erstelle_tagesauszug`) aus der Ausgabe gefiltert, nicht hier
    schon unterdrückt -- derselbe Katalog wie überall sonst (Festlegung 6.1.1)."""
    nav = valuation.total_value
    now = market.now()
    day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    month_start = day_start.replace(day=1)

    nav_hoechststand = await hoechststand(session, portfolio.id)
    if nav_hoechststand is not None:
        nav_hoechststand = max(nav_hoechststand, nav)
    nav_tagesanfang = await letzter_wert_vor(session, portfolio.id, day_start)
    nav_vor_7_tagen = await letzter_wert_vor(session, portfolio.id, now - timedelta(days=7))
    nav_monatsanfang = await letzter_wert_vor(session, portfolio.id, month_start)

    holdings_value = sum((p.market_value for p in valuation.positions), start=Decimal(0))
    anzahl_offener_positionen = sum(1 for p in valuation.positions if p.quantity != 0)
    asset_ids = [p.asset.id for p in valuation.positions if p.quantity != 0]
    stop_preise = await offene_stop_preise(session, portfolio.id, asset_ids)
    summenrisiko = summenrisiko_offener_positionen(valuation, stop_preise)
    hat_offene_positionen = anzahl_offener_positionen > 0
    korrelation = None if hat_offene_positionen else Decimal(0)

    return RisikoKontext(
        tranche=tranche,
        tranche_eur=tranche_eur,
        nav=nav,
        direction="long",  # kein Vorschlag -- short_verbot wird ohnehin gefiltert
        nav_hoechststand=nav_hoechststand,
        nav_tagesanfang=nav_tagesanfang,
        nav_vor_7_tagen=nav_vor_7_tagen,
        nav_monatsanfang=nav_monatsanfang,
        cash=valuation.cash,
        brutto_exposure=holdings_value,
        anzahl_offener_positionen=anzahl_offener_positionen,
        einzelwert_marktwert_nach_trade=None,
        summenrisiko_vor_trade=summenrisiko,
        summenrisiko_nach_trade=summenrisiko,
        korrelation_zu_offenen_positionen=korrelation,
        hat_offene_positionen=hat_offene_positionen,
        risiko_je_trade_eur=Decimal(0),
        positionsgroesse_implizierte_obergrenze=None,
        proposal_size=Decimal(0),
        nachkauf_in_verlust_pruefbar=False,
        position_unrealized_vor_trade_eur=None,
        cash_ausserhalb_boerse=portfolio.external_reserve_cash,
    )


async def _pnl_aufteilung(
    session: AsyncSession, market: MarketService, portfolio: Portfolio, valuation: Valuation,
    nav_tagesanfang: Decimal | None,
) -> PnlAufteilung:
    if nav_tagesanfang is None:
        return PnlAufteilung(
            berechenbar=False,
            grund="kein Tagesanfangs-Wertschnappschuss vorhanden (neues Portfolio oder "
            "erster Tag nach Einführung von B-11)",
            tages_pnl_gesamt=None, handelsergebnis=None, waehrungseffekt=None,
        )
    tages_pnl_gesamt = valuation.total_value - nav_tagesanfang

    now = market.now()
    day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    basis_snapshot = await letzter_snapshot_vor(session, portfolio.id, day_start)
    basis_positionen = (
        (basis_snapshot.breakdown or {}).get("positions", {}) if basis_snapshot is not None
        else {}
    )
    if not basis_positionen:
        return PnlAufteilung(
            berechenbar=False,
            grund="kein Tagesanfangs-Bestandsschnappschuss mit Positionsdetails vorhanden "
            "(vor B-11 geschriebene Schnappschüsse kennen nur die NAV-Summe, keine "
            "Positionsaufschlüsselung) -- Gesamtsumme ist trotzdem berechenbar, die "
            "Aufteilung erst ab dem nächsten Tagesschnappschuss",
            tages_pnl_gesamt=tages_pnl_gesamt, handelsergebnis=None, waehrungseffekt=None,
        )

    waehrungseffekt = Decimal(0)
    for position in valuation.positions:
        basis = basis_positionen.get(str(position.asset.id))
        if basis is None or position.mark.price is None or position.mark.price_base is None:
            # Neu eröffnete Position seit Tagesanfang, oder Kurs heute nicht lesbar:
            # keine Tagesanfangs-FX-Basis zum Vergleich -- ihr gesamter Wertbeitrag
            # bleibt im Handelsergebnis-Restbetrag unten, nicht fail closed für den
            # ganzen Auszug.
            continue
        fx_heute = position.mark.price_base / position.mark.price
        fx_tagesanfang = Decimal(basis["fx_rate"])
        waehrungseffekt += position.quantity * position.mark.price * (fx_heute - fx_tagesanfang)

    handelsergebnis = tages_pnl_gesamt - waehrungseffekt
    return PnlAufteilung(
        berechenbar=True, grund=None, tages_pnl_gesamt=tages_pnl_gesamt,
        handelsergebnis=handelsergebnis, waehrungseffekt=waehrungseffekt,
    )


def _anteil_je_quote_waehrung(valuation: Valuation, nav: Decimal) -> dict[str, Decimal]:
    werte: dict[str, Decimal] = {}
    for position in valuation.positions:
        if position.quantity == 0:
            continue
        currency = position.mark.currency
        werte[currency] = werte.get(currency, Decimal(0)) + position.market_value
    if nav == 0:
        return {k: Decimal(0) for k in werte}
    return {k: (v / nav * Decimal(100)) for k, v in werte.items()}


async def erstelle_tagesauszug(
    session: AsyncSession, market: MarketService, portfolio: Portfolio,
    *, tranche: str, tranche_eur: Decimal,
) -> Risikoauszug:
    """B-11/HED-83: der vollständige Tages-Risikoauszug für genau ein Portfolio."""
    valuation = await value_portfolio(session, market, portfolio)
    ctx = await _sammle_buchkontext(
        session, market, portfolio, valuation, tranche=tranche, tranche_eur=tranche_eur
    )
    alle_limits = pruefe_alle_limits(ctx)
    limits = [e for e in alle_limits if e.limit not in _NUR_ORDERZEITPUNKT]

    pnl = await _pnl_aufteilung(session, market, portfolio, valuation, ctx.nav_tagesanfang)

    cash_gesamt = ctx.cash + ctx.cash_ausserhalb_boerse
    barquote_pct = (cash_gesamt / ctx.nav * Decimal(100)) if ctx.nav > 0 else Decimal(0)

    # HED-8 nennt namentlich USD und USDT. USDC steht nicht im Wortlaut der mir
    # vorliegenden Fassung -- seit HED-99 handelt die Installation aber überwiegend
    # gegen USDC (OKX lehnt USDT im EWR-Konto ab), eine Spalte dafür zu
    # unterschlagen würde den größten Teil der Stablecoin-Exponierung unsichtbar
    # machen. Als zusätzliches Feld ausgewiesen, nicht in usd_/usdt_anteil_pct
    # eingerechnet -- CRO entscheidet, ob USDC künftig dieselbe Risikospalte wie
    # USDT teilen soll (Entkopplungsrisiko), siehe PR-Beschreibung.
    anteil_je_waehrung = _anteil_je_quote_waehrung(valuation, ctx.nav)

    asset_ids_offen = [p.asset.id for p in valuation.positions if p.quantity != 0]
    stop_preise = await offene_stop_preise(session, portfolio.id, asset_ids_offen)
    positionen_ohne_stop = [
        p.asset.symbol for p in valuation.positions
        if p.quantity != 0 and p.asset.id not in stop_preise
    ]

    return Risikoauszug(
        generated_at=market.now(),
        portfolio_id=portfolio.id,
        nav=ctx.nav,
        drawdown=(
            (ctx.nav_hoechststand - ctx.nav) if ctx.nav_hoechststand is not None else None
        ),
        drawdown_pct=(
            (ctx.nav_hoechststand - ctx.nav) / ctx.nav_hoechststand * Decimal(100)
            if ctx.nav_hoechststand is not None and ctx.nav_hoechststand > 0 else None
        ),
        pnl=pnl,
        brutto_exposure=ctx.brutto_exposure,
        summenrisiko=ctx.summenrisiko_nach_trade,
        cash_boerse=ctx.cash,
        cash_ausserhalb_boerse=ctx.cash_ausserhalb_boerse,
        cash_gesamt=cash_gesamt,
        barquote_pct=barquote_pct,
        anteil_je_quote_waehrung_pct=anteil_je_waehrung,
        usd_anteil_pct=anteil_je_waehrung.get("USD", Decimal(0)),
        usdt_anteil_pct=anteil_je_waehrung.get("USDT", Decimal(0)),
        usdc_anteil_pct=anteil_je_waehrung.get("USDC", Decimal(0)),
        anzahl_offener_positionen=ctx.anzahl_offener_positionen,
        positionen_ohne_stop=positionen_ohne_stop,
        limits=limits,
    )
