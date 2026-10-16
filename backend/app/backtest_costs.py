"""Kostenmodell für den Backtest: Gebühren, Slippage, Funding (HED-120, Gate-B-Bedingung B-4).

Baut nicht an `market-data-platform/backtest_engine.py` weiter. Jene Engine rechnet mit
`commission=0` (Kosten hart auf null) und liest EODHD-Kurse, die für Krypto nicht
freigegeben sind (HED-10 Abschnitt 4.4, Q15: "zurückgestellt, nicht gekauft") -- sie
erfüllt B-4 nicht und wird laut Bestandsaufnahme auf HED-116 auch nicht einfach
weiterverwendet. Dieses Modul liefert die Kostenfunktionen für die neue, OKX-basierte
Backtest-Engine, die in `paper-trading-platform` entsteht (HED-119 Datenpfad + dieses
Modul + Zusammenführung in HED-121/Teilaufgabe 3), gebaut auf der einzeln freigegebenen
`backtesting` 0.6.5 (AGPL-3.0, HED-10 Abschnitt 4.4, vier Auflagen: unverändert benutzen,
nur lokal, nicht öffentlich erreichbar, keine Weitergabe).

Drei Kostenarten, bewusst getrennt gehalten:

Gebühr
    OKX-Spot-Taker-/Maker-Sätze aus derselben Quelle wie der Live-/Papier-Pfad
    (`app.feeprofiles`, Profil ``exchange-spot``, Satz ``okx``) -- keine neue
    Recherche, keine zweite Zahl, die von der ersten abweichen kann. `backtesting`
    unterscheidet in seiner Commission-Callable nicht zwischen ausführenden
    (Taker) und ruhenden (Maker) Orders -- sie bekommt nur Größe und Preis
    (siehe `Backtest.__doc__`: ``func(order_size, price) -> float``). Jeder
    Fill wird deshalb als Taker gerechnet: die teurere der beiden Annahmen,
    fail-closed statt eine günstigere Maker-Quote zu unterstellen, die ein
    Market-/Stop-Fill real nie bekommt.

Slippage
    Eine Backtest-Annahme (angenommener Kostensatz je Round-Trip), AUSDRÜCKLICH getrennt
    von der Zweiphasen-Messung aus HED-49 (`app.slippage`): jene misst reale Zeitstempel
    im laufenden Ausführungspfad und ist laut eigenem Docstring bewusst nicht mit einem
    Backtest-/Papier-Wert vergleichbar. Historische OHLCV-Kerzen haben keinen Geld-/
    Briefkurs -- der Backtest braucht deshalb IMMER eine Annahme, nicht nur als
    Rückfallwert wie `FeeTerms.assumed_spread` im Live-/Papier-Pfad. Anders als dessen
    Konvention (halbe Spanne gegen den Trader je Order-Seite, siehe `fees.fill_price`)
    berechnet `backtesting` 0.6.5 seinen ``spread``-Parameter nur EINMAL, beim Einstieg
    (Quelltext, `Broker._process_orders`: "Existing trades are closed at unadjusted
    price, because the adjustment was already made when buying.") -- der hier verwendete
    Wert ist deshalb bewusst als VOLLER Round-Trip-Kostensatz definiert, nicht als halbe
    Spanne, und geht unverändert in `Backtest(..., spread=...)`. `ASSUMED_SPREAD_
    SNAPSHOT_2026_10_16` unten ist eine echte Messung (Quelle, Methode, Datum stehen
    dabei); `DEFAULT_BACKTEST_SPREAD` ist davon abgeleitet, aber bewusst höher angesetzt
    -- als Schätzung gekennzeichnet, nicht als Messung, weil die Messung nur eine
    Momentaufnahme des besten Taxkurses in ruhigem Markt ist, keine Serie über schwache
    Liquidität, Volatilitätsspitzen oder größere Ordergrößen.

Funding
    Echter, verdrahteter Parameter, keine fehlende Größe. Bis Gate C gilt für den
    Live-Handel "nur Krypto-Spot auf OKX, nur Long, Hebel 1,0, keine Derivate, kein
    Short" (Risikopolitik 1.1 Abschnitt 4.5, Beschluss 2, HED-8) -- ein Perpetual-Swap-
    Funding-Zahlungsmechanismus existiert für keine heute handelbare Position. Family A
    (HED-9 Abschnitt 5.2) ist eine Spot-Strategie auf genau diesem Universum, deshalb
    ist ihre Funding-Rate strukturell 0, nicht einfach vergessen. `funding_rate_per_hour`
    wirft für jede andere Familie einen Fehler, statt leise 0 zurückzugeben -- eine
    künftige Perpetual-Familie muss diese Funktion erst erweitern, bevor sie läuft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.feeprofiles import load_seed
from app.trading.fees import FeeTerms, compute_fee

EXCHANGE_SPOT_PROFILE_KEY = "exchange-spot"
PRICING_SOURCE_OKX = "okx"

# Gemessen: ccxt `okx().fetch_ticker(symbol)`, bid/ask der besten Taxe, Momentaufnahme
# 2026-10-16 (Methode: (ask - bid) / Mittelkurs). Keine Zeitreihe, kein Nachweis für
# dünne Liquidität oder Stressphasen -- siehe Moduldocstring. Dient nur als Beleg für
# die Größenordnung hinter DEFAULT_BACKTEST_SPREAD, nicht als die Backtest-Annahme
# selbst.
ASSUMED_SPREAD_SNAPSHOT_2026_10_16: dict[str, Decimal] = {
    "BTC/USDC": Decimal("0.0000012"),  # bid 81875.1 / ask 81875.2
    "ETH/USDC": Decimal("0.0000041"),  # bid 2456.79 / ask 2456.80
    "SOL/USDC": Decimal("0.0001896"),  # bid 105.53 / ask 105.55
}
SPREAD_MEASUREMENT_DATE = date(2026, 10, 16)

# Schätzung (nicht Messung): angenommener Slippage-Kostensatz für einen vollen
# Round-Trip (Kauf + Verkauf), deutlich über der Momentaufnahme oben, um
# Ruhephasen-Verzerrung nicht zu unterschätzen. Je Symbol in derselben Größenordnung
# wie die bereits geprüften Broker-Schätzungen im Live-/Papier-Pfad (`ibkr-fixed-us`:
# 0.0002, `ibkr-fx`: 0.0001, siehe fee_profiles.yaml) -- für die weniger liquiden Alts
# bewusst höher. Der Model Validator kann diese Zahlen bei der B-4-Abnahme anfechten;
# sie sind als Konstanten bewusst an einer Stelle versammelt, nicht über den Code
# verteilt.
DEFAULT_BACKTEST_SPREAD: dict[str, Decimal] = {
    "BTC/USDC": Decimal("0.0005"),
    "ETH/USDC": Decimal("0.0005"),
    "SOL/USDC": Decimal("0.0010"),
}
# Für ein Symbol ohne eigenen Eintrag: die vorsichtigere der beiden obigen Zahlen.
FALLBACK_BACKTEST_SPREAD = Decimal("0.0010")

FAMILY_A = "A"


class UngebauteFamilie(NotImplementedError):
    """Funding für eine Asset-Familie, die dieses Modul noch nicht kennt."""


def _okx_spot_profile_entry() -> dict:
    """Der ``exchange-spot``-Eintrag aus `app/seed/fee_profiles.yaml`, ungelesen aus der DB.

    Dieselbe Quelle wie der Live-/Papier-Pfad (`app.feeprofiles.terms_for`) -- ändert
    sich die dort gepflegte Zahl, zieht der Backtest automatisch nach, ohne eine zweite
    Abschrift, die auseinanderlaufen könnte.
    """
    for entry in load_seed():
        if entry["key"] == EXCHANGE_SPOT_PROFILE_KEY:
            return entry
    raise LookupError(f"Fee-Profil {EXCHANGE_SPOT_PROFILE_KEY!r} fehlt in fee_profiles.yaml")


def okx_spot_fee_terms() -> FeeTerms:
    """OKX-Spot-Gebührensätze (Taker 0,10 %, Maker 0,08 % zum Prüfdatum des Profils)."""
    entry = _okx_spot_profile_entry()
    published = entry["exchange_rates"][PRICING_SOURCE_OKX]
    return FeeTerms(
        profile_key=entry["key"],
        profile_name=entry["name"],
        maker_rate=Decimal(published["maker"]),
        taker_rate=Decimal(published["taker"]),
    )


def backtest_assumed_spread(symbol: str) -> Decimal:
    """Angenommener Slippage-Kostensatz für einen vollen Round-Trip in ``symbol``.

    Schätzung, siehe die Konstanten oben.
    """
    return DEFAULT_BACKTEST_SPREAD.get(symbol, FALLBACK_BACKTEST_SPREAD)


def funding_rate_per_hour(asset_family: str) -> Decimal:
    """Finanzierungskosten je Stunde offener Position, als Anteil des Positionswerts.

    Family A (Spot, kein Perpetual) hat strukturell keine Funding-Rate -- siehe
    Moduldocstring. Jede andere Familie muss diese Funktion zuerst erweitern; ein
    impliziter Nullwert für eine unbekannte Familie wäre genau die Lücke, die B-4 bei
    einer künftigen Perpetual-Familie wieder öffnen würde.
    """
    if asset_family == FAMILY_A:
        return Decimal(0)
    raise UngebauteFamilie(
        f"Funding für Asset-Familie {asset_family!r} ist nicht gebaut. B-4 ist nur für "
        f"Family A (Spot) erfüllt; eine Perpetual-Familie braucht eine echte, "
        f"börsenseitig gemessene Rate, keinen impliziten Nullwert."
    )


@dataclass(frozen=True, slots=True)
class BacktestCostModel:
    """Alle drei Kostenarten für ein Symbol, fertig für `backtesting.Backtest(...)`.

    Verwendung (Zusammenführung in HED-121):
        model = cost_model_for("BTC/USDC")
        bt = Backtest(df, Strategy, commission=model.commission,
                       spread=model.backtesting_spread_param)
    """

    symbol: str
    asset_family: str
    fee_terms: FeeTerms
    assumed_spread: Decimal  # voller Round-Trip-Kostensatz, siehe Moduldocstring "Slippage"
    funding_rate_per_hour: Decimal

    @property
    def backtesting_spread_param(self) -> float:
        """Wert für `Backtest(..., spread=...)`.

        `backtesting` 0.6.5 belastet seinen ``spread``-Parameter nur beim Einstieg, nicht
        beim Ausstieg (Quelltext, `Broker._process_orders`: "Existing trades are closed
        at unadjusted price, because the adjustment was already made when buying.") --
        eine einzige Anwendung ergibt also bereits den vollen Round-Trip-Kostensatz.
        `assumed_spread` ist genau so definiert; deshalb hier unverändert durchreichen,
        nicht halbieren.
        """
        return float(self.assumed_spread)

    def commission(self, order_size: float, price: float) -> float:
        """Commission-Callable für `backtesting.Backtest(..., commission=...)`."""
        quantity = abs(Decimal(str(order_size)))
        value = quantity * Decimal(str(price))
        fee = compute_fee(self.fee_terms, value=value, quantity=quantity, liquidity="taker")
        return float(fee)


def cost_model_for(symbol: str, *, asset_family: str = FAMILY_A) -> BacktestCostModel:
    """Das vollständige Kostenmodell für ``symbol`` -- Gebühr, Slippage, Funding."""
    return BacktestCostModel(
        symbol=symbol,
        asset_family=asset_family,
        fee_terms=okx_spot_fee_terms(),
        assumed_spread=backtest_assumed_spread(symbol),
        funding_rate_per_hour=funding_rate_per_hour(asset_family),
    )
