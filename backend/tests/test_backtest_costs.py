"""HED-120: Kostenmodell im Backtest -- Gebühren, Slippage, Funding (B-4).

Prüft drei Dinge: die Gebührenzahlen stimmen mit der Live-/Papier-Quelle überein (keine
zweite, abweichende Abschrift), die Slippage-Annahme ist vom Symbol abhängig und
ausdrücklich keine Wiederverwendung von HED-49, und Funding ist für Family A echt 0,
nicht für jede Familie. Der letzte Test führt einen echten `backtesting.Backtest` mit
dem kompletten Kostenmodell gegen eine synthetische Kursreihe, damit die Commission-
Callable und der ``spread``-Parameter nachweislich so funktionieren, wie die
Bibliothek sie erwartet -- nicht nur, wie dieses Modul sie beschreibt.
"""

from decimal import Decimal

import pandas as pd
import pytest
from backtesting import Backtest, Strategy

from app.backtest_costs import (
    DEFAULT_BACKTEST_SPREAD,
    FALLBACK_BACKTEST_SPREAD,
    FAMILY_A,
    UngebauteFamilie,
    backtest_assumed_spread,
    cost_model_for,
    funding_rate_per_hour,
    okx_spot_fee_terms,
)
from app.feeprofiles import load_seed
from app.trading.fees import compute_fee
from tests.conftest import D

# --- Gebühr: dieselbe Quelle wie der Live-/Papier-Pfad ------------------------------


def test_okx_spot_fee_terms_match_the_seed_file():
    terms = okx_spot_fee_terms()
    exchange = next(e for e in load_seed() if e["key"] == "exchange-spot")
    published = exchange["exchange_rates"]["okx"]

    assert terms.maker_rate == D(published["maker"])
    assert terms.taker_rate == D(published["taker"])
    assert terms.profile_key == "exchange-spot"


def test_okx_spot_fee_terms_are_not_a_second_number():
    """Ändert sich der Live-/Papier-Satz, zieht der Backtest automatisch nach."""
    changed = load_seed()
    exchange = next(e for e in changed if e["key"] == "exchange-spot")
    exchange["exchange_rates"]["okx"]["taker"] = "0.0099"

    # okx_spot_fee_terms() liest load_seed() frisch, nicht die Kopie oben --
    # das beweist nur, dass beide aus derselben Datei kommen, nicht aus zwei Zahlen.
    terms = okx_spot_fee_terms()
    assert terms.taker_rate == D(
        next(e for e in load_seed() if e["key"] == "exchange-spot")["exchange_rates"]["okx"][
            "taker"
        ]
    )


def test_commission_callable_charges_the_taker_rate():
    model = cost_model_for("BTC/USDC")

    fee = model.commission(order_size=2, price=50_000)

    assert fee == float(
        compute_fee(model.fee_terms, value=D(100_000), quantity=D(2), liquidity="taker")
    )


def test_commission_callable_uses_the_absolute_order_size():
    """`backtesting` gibt bei Short-Orders eine negative Größe -- die Gebühr bleibt positiv."""
    model = cost_model_for("BTC/USDC")

    assert model.commission(order_size=-2, price=50_000) == model.commission(
        order_size=2, price=50_000
    )


# --- Slippage: Backtest-Annahme, keine Wiederverwendung von HED-49 ------------------


def test_assumed_spread_is_symbol_specific():
    assert backtest_assumed_spread("BTC/USDC") == DEFAULT_BACKTEST_SPREAD["BTC/USDC"]
    assert backtest_assumed_spread("SOL/USDC") == DEFAULT_BACKTEST_SPREAD["SOL/USDC"]
    assert backtest_assumed_spread("SOL/USDC") > backtest_assumed_spread("BTC/USDC")


def test_assumed_spread_falls_back_for_an_unlisted_symbol():
    assert backtest_assumed_spread("DOGE/USDC") == FALLBACK_BACKTEST_SPREAD


def test_backtesting_spread_param_equals_the_full_assumed_spread():
    """`backtesting` belastet seinen ``spread``-Parameter nur beim Einstieg (siehe
    Docstring von `backtesting_spread_param`) -- eine Anwendung ist bereits der volle
    Round-Trip-Kostensatz, also keine Halbierung hier."""
    model = cost_model_for("BTC/USDC")

    assert model.backtesting_spread_param == pytest.approx(float(model.assumed_spread))


def test_slippage_module_does_not_import_the_live_measurement():
    """HED-49 (`app.slippage`) misst reale Zeitstempel und ist laut eigenem Docstring
    nicht mit einem Backtest-Wert vergleichbar -- dieses Modul darf es nicht benutzen."""
    import app.backtest_costs as module

    assert "slippage" not in module.__file__
    assert not hasattr(module, "berechne_phase1")
    assert not hasattr(module, "berechne_phase2")


# --- Funding: echter Parameter, nicht einfach fehlend -------------------------------


def test_funding_is_zero_for_family_a():
    assert funding_rate_per_hour(FAMILY_A) == Decimal(0)


def test_funding_raises_for_an_unbuilt_family():
    with pytest.raises(UngebauteFamilie):
        funding_rate_per_hour("B")


def test_cost_model_for_carries_the_funding_rate():
    model = cost_model_for("BTC/USDC")
    assert model.funding_rate_per_hour == Decimal(0)
    assert model.asset_family == FAMILY_A

    with pytest.raises(UngebauteFamilie):
        cost_model_for("BTC/USDC", asset_family="B")


# --- Ende-zu-Ende: ein echter `backtesting.Backtest` mit dem Kostenmodell -----------


def _ohlcv(prices: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(prices), freq="D")
    return pd.DataFrame(
        {
            "Open": prices,
            "High": prices,
            "Low": prices,
            "Close": prices,
            "Volume": [1.0] * len(prices),
        },
        index=index,
    )


class _BuyThenSell(Strategy):
    """Kauft an Tag 2, verkauft an Tag 6 -- ein vollständiger, regulärer Round-Trip."""

    def init(self):
        pass

    def next(self):
        if len(self.data) == 2:
            self.buy()
        if len(self.data) == 6:
            self.position.close()


def test_backtest_charges_fee_and_spread_on_a_synthetic_series():
    # Flacher Kurs, damit die reine Kostenwirkung sichtbar bleibt, keine Marktbewegung.
    df = _ohlcv([100.0] * 10)
    model = cost_model_for("BTC/USDC")

    bt = Backtest(
        df,
        _BuyThenSell,
        cash=10_000,
        commission=model.commission,
        spread=model.backtesting_spread_param,
        trade_on_close=True,
    )
    stats = bt.run()

    trade = stats["_trades"].iloc[0]
    taker_rate = float(model.fee_terms.taker_rate)
    spread = model.backtesting_spread_param
    size = trade["Size"]

    # Entry: Preis um den vollen Round-Trip-Spread gegen den Trader verschoben (einmalig,
    # siehe Docstring von `backtesting_spread_param`); Exit unadjustiert.
    assert trade["EntryPrice"] == pytest.approx(100.0 * (1 + spread), rel=1e-9)
    assert trade["ExitPrice"] == pytest.approx(100.0, rel=1e-9)

    # Kein Kursausschlag: der gesamte PnL-Verlust ist genau Gebühr (beide Seiten) + Spread.
    expected_fee = 2 * taker_rate * abs(size) * 100.0
    expected_spread_cost = abs(size) * 100.0 * spread
    assert trade["PnL"] == pytest.approx(-(expected_fee + expected_spread_cost), rel=1e-6)
    assert stats["Equity Final [$]"] == pytest.approx(10_000 + trade["PnL"], rel=1e-9)
