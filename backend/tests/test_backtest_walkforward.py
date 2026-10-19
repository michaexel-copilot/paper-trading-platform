"""HED-121: Walk-Forward-Rahmen über mindestens vier Fenster (P5).

Dieselbe Prüfphilosophie wie HED-120 (test_backtest_costs.py): ein echter
`backtesting.Backtest` auf synthetischen, flachen oder zick-zack-förmigen Kursreihen
mit einer winzigen, deterministischen Strategie -- keine Forschungsstrategie, nur
genug, um die Fenster-Geometrie und die Kostenweitergabe nachzuweisen.
"""

import pandas as pd
import pytest
from backtesting import Backtest, Strategy

from app.backtest_costs import cost_model_for
from app.backtest_walkforward import (
    MIN_WINDOWS,
    InsufficientHistory,
    WindowSpec,
    build_windows,
    run_final_holdout,
    run_walk_forward,
)

# --- WindowSpec: die Mindestfenster-Vorgabe (HED-9 Abschnitt 7, P5) ist erzwungen ---


def test_window_spec_rejects_fewer_than_the_minimum_windows():
    assert MIN_WINDOWS == 4
    with pytest.raises(ValueError, match="mindestens 4"):
        WindowSpec(n_windows=3, train_bars=10, test_bars=5)


def test_window_spec_rejects_non_positive_sizes():
    with pytest.raises(ValueError):
        WindowSpec(n_windows=4, train_bars=0, test_bars=5)
    with pytest.raises(ValueError):
        WindowSpec(n_windows=4, train_bars=10, test_bars=0)
    with pytest.raises(ValueError):
        WindowSpec(n_windows=4, train_bars=10, test_bars=5, step_bars=0)


def test_effective_step_defaults_to_test_bars():
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    assert spec.effective_step == 5


def test_required_bars_matches_the_last_window_end():
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    # Fenster 0: [0,10)/[10,15); Fenster 3: train ab 15, [15,25)/[25,30)
    assert spec.required_bars() == 30


# --- build_windows: reine Geometrie, rollierend und verankert -----------------------


def test_rolling_windows_slide_forward_without_overlapping_test_slices():
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    windows = build_windows(30, spec)

    assert len(windows) == 4
    assert [w.train for w in windows] == [slice(0, 10), slice(5, 15), slice(10, 20), slice(15, 25)]
    assert [w.test for w in windows] == [slice(10, 15), slice(15, 20), slice(20, 25), slice(25, 30)]
    # Testabschnitte berühren sich, überlappen aber nie.
    for a, b in zip(windows, windows[1:], strict=False):
        assert a.test.stop == b.test.start


def test_anchored_windows_grow_the_training_slice_from_the_start():
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5, anchored=True)
    windows = build_windows(30, spec)

    assert [w.train for w in windows] == [slice(0, 10), slice(0, 15), slice(0, 20), slice(0, 25)]
    assert [w.test for w in windows] == [slice(10, 15), slice(15, 20), slice(20, 25), slice(25, 30)]


def test_step_bars_can_differ_from_test_bars_to_overlap_or_skip():
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5, step_bars=10)
    windows = build_windows(55, spec)

    assert [w.test for w in windows] == [slice(10, 15), slice(20, 25), slice(30, 35), slice(40, 45)]


def test_build_windows_raises_insufficient_history_with_the_exact_shortfall():
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    with pytest.raises(InsufficientHistory, match="30"):
        build_windows(29, spec)


def test_build_windows_raises_for_the_measured_usdc_depth_against_multi_year_sizing():
    """HED-125 ist absichtlich nicht hier entschieden: eine Fenstergröße, die auf
    "mehrere Jahre" Historie ausgelegt ist, muss an der gemessenen ~14-Monats-USDC-
    Tiefe (HED-119-Entwurf) klar scheitern, nicht leise mit kürzeren Fenstern laufen."""
    measured_usdc_daily_bars = 430  # ~14 Monate Tageskerzen, siehe HED-119/HED-125
    spec = WindowSpec(n_windows=4, train_bars=365, test_bars=90)  # "mehrere Jahre"-Annahme

    with pytest.raises(InsufficientHistory):
        build_windows(measured_usdc_daily_bars, spec)


# --- run_walk_forward: echte Backtest.run() je Fenster, Kosten auf jedem Fill -------


def _zigzag_ohlcv(n: int, *, amplitude: float = 5.0, base: float = 100.0) -> pd.DataFrame:
    """Deterministische Sägezahn-Kursreihe: gerade Tage steigen, ungerade fallen --
    liefert sowohl Gewinn- als auch Verlust-Trades für eine Trefferquote <100 %."""
    prices = [base + (amplitude if i % 2 == 0 else -amplitude) for i in range(n)]
    index = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "Open": prices,
            "High": [p + 1 for p in prices],
            "Low": [p - 1 for p in prices],
            "Close": prices,
            "Volume": [1.0] * n,
        },
        index=index,
    )


class _DailyFlip(Strategy):
    """Kauft, wenn flach/im Minus, verkauft am nächsten Balken -- rein technisch, um
    in jedem Fenster mehrere abgeschlossene Trades zu erzeugen. Keine Forschungslogik."""

    def init(self):
        pass

    def next(self):
        if self.position:
            self.position.close()
        else:
            self.buy()


def test_run_walk_forward_produces_per_window_metrics_and_a_summary():
    frame = _zigzag_ohlcv(30)
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("BTC/USDC")

    result = run_walk_forward(frame, _DailyFlip, model, spec)

    assert len(result.windows) == 4
    for w in result.windows:
        assert w.trade_count > 0
        assert isinstance(w.pnl, float)
        assert 0.0 <= w.win_rate_pct <= 100.0
        assert w.test_start < w.test_end

    summary = result.summary()
    assert summary["n_windows"] == 4
    assert summary["total_trades"] == sum(w.trade_count for w in result.windows)
    assert summary["total_pnl"] == pytest.approx(sum(w.pnl for w in result.windows))
    assert summary["windows_with_a_trade"] == 4


def test_run_walk_forward_charges_the_cost_model_on_every_window():
    """Dieselbe Strategie, einmal mit dem echten Kostenmodell, einmal mit Kosten auf
    null -- der PnL-Unterschied beweist, dass die Kosten tatsächlich je Fill greifen,
    nicht nur in der Beschreibung des Kostenmodells selbst (wie HED-120s e2e-Test)."""
    frame = _zigzag_ohlcv(30)
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("SOL/USDC")  # höhere Default-Spread-Annahme als BTC/ETH

    with_costs = run_walk_forward(frame, _DailyFlip, model, spec)

    from dataclasses import replace

    zero_cost_model = replace(
        model,
        assumed_spread=model.assumed_spread.__class__(0),
        fee_terms=replace(
            model.fee_terms,
            maker_rate=model.fee_terms.maker_rate.__class__(0),
            taker_rate=model.fee_terms.taker_rate.__class__(0),
        ),
    )
    without_costs = run_walk_forward(frame, _DailyFlip, zero_cost_model, spec)

    for with_window, without_window in zip(with_costs.windows, without_costs.windows, strict=True):
        if with_window.trade_count > 0:
            assert with_window.pnl < without_window.pnl


def test_finalize_trades_changes_the_exact_trade_count_by_the_one_position_left_open():
    """Nachforderung der CRO-Abnahme (HED-133): die Begründung für
    `finalize_trades=True` in `run_walk_forward`/`run_final_holdout` (Kommentar dort)
    beweist mit einer exakten Zahl, nicht nur einem Verhalten, dass genau die am
    Fensterende offene Position den Unterschied macht -- nicht mehr, nicht weniger."""
    test_df = _zigzag_ohlcv(5)
    model = cost_model_for("BTC/USDC")
    kwargs = dict(cash=10_000.0, commission=model.commission, spread=model.backtesting_spread_param)

    left_open = Backtest(test_df, _DailyFlip, finalize_trades=False, **kwargs).run()
    force_closed = Backtest(test_df, _DailyFlip, finalize_trades=True, **kwargs).run()

    assert int(force_closed["# Trades"]) == int(left_open["# Trades"]) + 1

    # run_walk_forward erzwingt intern finalize_trades=True -- dieselbe Zahl, nicht die
    # von finalize_trades=False.
    full_frame = _zigzag_ohlcv(30)
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    result = run_walk_forward(full_frame, _DailyFlip, model, spec)
    assert result.windows[0].trade_count == int(force_closed["# Trades"])
    assert result.windows[0].trade_count != int(left_open["# Trades"])


def test_run_walk_forward_calls_fit_on_the_training_slice_of_each_window():
    """``fit`` ist ein reiner Durchreiche-Hook: dieses Modul ruft ihn nur auf und gibt
    das Ergebnis an ``Backtest.run(**params)`` weiter -- keine eigene Optimierung."""
    frame = _zigzag_ohlcv(30)
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("BTC/USDC")
    seen_train_slices = []

    def fit(train_df: pd.DataFrame) -> dict:
        seen_train_slices.append((train_df.index[0], train_df.index[-1], len(train_df)))
        return {}

    result = run_walk_forward(frame, _DailyFlip, model, spec, fit=fit)

    assert len(seen_train_slices) == 4
    assert all(count == 10 for *_ts, count in seen_train_slices)
    for window_result, (train_start, train_end, _count) in zip(
        result.windows, seen_train_slices, strict=True
    ):
        assert window_result.train_start == train_start
        assert window_result.train_end == train_end


def test_window_without_any_trade_reports_nan_win_rate_and_is_excluded_from_the_mean():
    """Ein flacher Markt mit einer Strategie, die nie handelt: 0 Trades ist ein echter,
    zulässiger Zustand (nicht derselbe wie "kein Risiko eingegangen" bei B-2) und darf
    die mittlere Trefferquote nicht verzerren."""
    frame = _zigzag_ohlcv(30)
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("BTC/USDC")

    class _NeverTrade(Strategy):
        def init(self):
            pass

        def next(self):
            pass

    result = run_walk_forward(frame, _NeverTrade, model, spec)

    assert all(w.trade_count == 0 for w in result.windows)
    assert all(w.win_rate_pct != w.win_rate_pct for w in result.windows)  # NaN != NaN
    assert result.mean_win_rate_pct != result.mean_win_rate_pct  # NaN, keine 0-Trades-Täuschung
    assert result.summary()["windows_with_a_trade"] == 0


# --- label: sichtbare Kennzeichnung der Datenquelle (Board-Entscheidung HED-125) ----


def test_label_is_carried_through_to_the_summary():
    frame = _zigzag_ohlcv(30)
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("BTC/USDC")

    result = run_walk_forward(frame, _DailyFlip, model, spec, label="Proxy-Historie USDT")

    assert result.label == "Proxy-Historie USDT"
    assert result.summary()["label"] == "Proxy-Historie USDT"


def test_label_defaults_to_none_when_not_given():
    frame = _zigzag_ohlcv(30)
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("BTC/USDC")

    result = run_walk_forward(frame, _DailyFlip, model, spec)

    assert result.label is None
    assert result.summary()["label"] is None


# --- run_final_holdout: ein einziges, festes Out-of-Sample-Fenster (HED-125 Punkt 2) -


def test_run_final_holdout_runs_a_single_fixed_window_without_rolling():
    holdout_frame = _zigzag_ohlcv(10)
    model = cost_model_for("BTC/USDC")

    result = run_final_holdout(holdout_frame, _DailyFlip, model, label="echte USDC-Monate")

    assert result.trade_count > 0
    assert isinstance(result.pnl, float)
    assert result.train_start is None and result.train_end is None
    assert result.test_start == holdout_frame.index[0]
    assert result.test_end == holdout_frame.index[-1]
    assert result.label == "echte USDC-Monate"
    assert result.summary() == {
        "label": "echte USDC-Monate",
        "pnl": result.pnl,
        "win_rate_pct": result.win_rate_pct,
        "trade_count": result.trade_count,
    }


def test_run_final_holdout_fits_on_the_train_frame_when_given():
    train_frame = _zigzag_ohlcv(10)
    holdout_frame = _zigzag_ohlcv(10)
    model = cost_model_for("BTC/USDC")
    seen = []

    def fit(df: pd.DataFrame) -> dict:
        seen.append(df)
        return {}

    result = run_final_holdout(holdout_frame, _DailyFlip, model, train_frame=train_frame, fit=fit)

    assert len(seen) == 1
    assert seen[0] is train_frame
    assert result.train_start == train_frame.index[0]
    assert result.train_end == train_frame.index[-1]


def test_final_holdout_is_not_rolled_even_if_it_would_fit_more_than_one_window():
    """Anders als `run_walk_forward` zerlegt `run_final_holdout` seine Eingabe nie in
    mehrere Fenster -- die ganze übergebene Reihe ist EIN Test, wie von der Board-
    Entscheidung verlangt ("kein weiteres Rollieren darüber")."""
    holdout_frame = _zigzag_ohlcv(40)  # groß genug für mehrere Fenster, wird trotzdem nicht geteilt
    model = cost_model_for("BTC/USDC")

    result = run_final_holdout(holdout_frame, _DailyFlip, model)

    assert result.test_start == holdout_frame.index[0]
    assert result.test_end == holdout_frame.index[-1]


# --- HED-180: FractionalBacktest statt Backtest, sonst stille Order-Verwerfung -----


def _zigzag_ohlcv_at_price(n: int, *, base: float, amplitude: float) -> pd.DataFrame:
    """Wie ``_zigzag_ohlcv``, aber mit frei wählbarem Preisniveau -- um ein Preisniveau
    deutlich über dem Standard-Testkapital (10 000 USD) zu erzeugen, wie bei echten
    BTC/ETH-Kursen (HED-178, HED-174 Abschnitt 9)."""
    prices = [base + (amplitude if i % 2 == 0 else -amplitude) for i in range(n)]
    index = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "Open": prices,
            "High": [p + amplitude / 5 for p in prices],
            "Low": [p - amplitude / 5 for p in prices],
            "Close": prices,
            "Volume": [1.0] * n,
        },
        index=index,
    )


def test_run_walk_forward_does_not_silently_drop_trades_at_a_high_single_unit_price():
    """HED-180 (Nachforderung CRO zu HED-121/HED-178): ``backtesting.Backtest`` stückelt
    ganzzahlig und verwirft Kauforders bei einem Einzelpreis deutlich über dem
    Testkapital lautlos als "insufficient margin" (keine Exception, nur eine Warnung).
    Bei einem Preis von 80 000 (realistisch für BTC) gegen das Standard-Testkapital von
    10 000 USD hätte das unkorrigierte ``Backtest`` hier 0 Trades gezählt (gemessen oben
    per Handprobe) -- ``run_walk_forward`` muss stattdessen dieselbe Trade-Zahl wie bei
    einem unauffälligen, niedrigen Preisniveau liefern."""
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("BTC/USDC")

    low_price_frame = _zigzag_ohlcv_at_price(30, base=100.0, amplitude=5.0)
    high_price_frame = _zigzag_ohlcv_at_price(30, base=80_000.0, amplitude=4_000.0)

    low_price_result = run_walk_forward(low_price_frame, _DailyFlip, model, spec)
    high_price_result = run_walk_forward(high_price_frame, _DailyFlip, model, spec)

    assert low_price_result.total_trades > 0
    assert high_price_result.total_trades == low_price_result.total_trades
    for w in high_price_result.windows:
        assert w.trade_count > 0


def test_run_final_holdout_does_not_silently_drop_trades_at_a_high_single_unit_price():
    """Dieselbe Regression wie oben, für ``run_final_holdout`` (eigener Aufrufpunkt,
    eigene ``Backtest``/``FractionalBacktest``-Instanz)."""
    model = cost_model_for("BTC/USDC")

    low_price_frame = _zigzag_ohlcv_at_price(10, base=100.0, amplitude=5.0)
    high_price_frame = _zigzag_ohlcv_at_price(10, base=80_000.0, amplitude=4_000.0)

    low_price_result = run_final_holdout(low_price_frame, _DailyFlip, model)
    high_price_result = run_final_holdout(high_price_frame, _DailyFlip, model)

    assert low_price_result.trade_count > 0
    assert high_price_result.trade_count == low_price_result.trade_count


def test_proxy_windows_and_real_final_holdout_compose_the_board_methodology():
    """Demonstriert die vom Board verlangte Zusammensetzung (HED-125, Hybrid): mehrere
    rollierende Fenster auf einer (hier synthetischen) Proxy-Reihe, gefolgt von einem
    einzigen festen Holdout auf einer separaten, "echten" Reihe -- beide mit eigenem,
    sichtbarem Label. Reine Verdrahtung, keine Entscheidung über echte Symbole hier."""
    proxy_frame = _zigzag_ohlcv(30)  # staende fuer die lange USDT-Proxy-Historie
    real_frame = _zigzag_ohlcv(8)  # staende fuer die echten USDC-Monate
    spec = WindowSpec(n_windows=4, train_bars=10, test_bars=5)
    model = cost_model_for("BTC/USDC")

    rolling = run_walk_forward(proxy_frame, _DailyFlip, model, spec, label="Proxy-Historie USDT")
    holdout = run_final_holdout(
        real_frame, _DailyFlip, model, train_frame=proxy_frame, label="echte USDC-Monate"
    )

    assert rolling.label != holdout.label
    assert len(rolling.windows) == 4
    assert holdout.test_start == real_frame.index[0]
