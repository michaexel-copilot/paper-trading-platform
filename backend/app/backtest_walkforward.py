"""Walk-Forward-Rahmen für den Backtest: mindestens vier Fenster statt eines einzelnen
80/20-Schnitts (HED-121, Research-Roadmap HED-9 Abschnitt 7, P5).

Reines Werkzeug, keine Strategie und keine Parameteroptimierung: dieses Modul kennt
weder ein Symbol noch eine Hypothese. Es nimmt eine fertige OHLCV-Zeitreihe (Format
wie ``app.backtest_data.historical.HistoryResult.frame`` oder jede andere
Open/High/Low/Close/Volume-DataFrame mit DatetimeIndex), ein Kostenmodell aus
``app.backtest_costs`` und eine ``backtesting.Strategy``-Klasse, zerlegt die Reihe in
mindestens vier Trainings-/Test-Fenster und führt für jedes Fenster einen echten
``backtesting.Backtest`` auf der Test-(Out-of-Sample-)Teilreihe aus, mit den Kosten aus
HED-120 auf jedem simulierten Fill. Das eigentliche Anpassen von Strategie-Parametern
auf der Trainings-Teilreihe ist Sache des Aufrufers (``fit``-Hook, siehe
``run_walk_forward``) -- das ist Quant-Researcher-Arbeit, keine, die dieses Modul
selbst trifft.

Offene Methodik-Frage, absichtlich NICHT hier entschieden (HED-125, an
CRO/Quant Researcher/Board verwiesen): Family A hat auf OKX für die handelbaren
USDC-Paare nur ~14 Monate Historie, während ein USDT-Proxy ~8,5 Jahre lieferte. Dieses
Modul trifft keine Annahme über die verfügbare Historientiefe -- Fenstergröße (``train_
bars``/``test_bars``) und Fensteranzahl sind Pflichtparameter, die der Aufrufer explizit
setzt. Reicht die übergebene Zeitreihe nicht für die gewünschten Fenster, wird das als
Fehler gemeldet (``InsufficientHistory``), nicht stillschweigend mit weniger oder
kürzeren Fenstern aufgefüllt -- fail closed, genau wie B-2 es für den Orderpfad
verlangt, nur hier auf der Validierungsmethodik angewendet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from backtesting import Backtest, Strategy

from app.backtest_costs import BacktestCostModel

# HED-9 Abschnitt 7, P5: "mindestens 4 Fenster" ist eine Methodik-Vorgabe des Boards,
# keine abwägbare Engineering-Wahl -- deshalb hier hart erzwungen, nicht nur empfohlen.
MIN_WINDOWS = 4


class InsufficientHistory(ValueError):
    """Die übergebene Zeitreihe hat zu wenig Balken für die gewünschten Fenster.

    Absichtlich ein Fehler, keine automatische Verkleinerung der Fenster: welche
    Fenstergröße für Family A richtig ist, hängt von der noch offenen HED-125-
    Entscheidung (USDC- vs. USDT-Proxy-Historie) ab, die dieses Modul nicht trifft.
    """


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """Geometrie der Walk-Forward-Fenster. Jeder Wert ist eine Balkenanzahl (Bars),
    keine Zeitspanne -- die Bedeutung eines Balkens (Tag, Stunde) ergibt sich aus der
    übergebenen Zeitreihe.

    ``anchored=False`` (rollierend, Standard): das Trainingsfenster hat feste Größe
    ``train_bars`` und wandert mit dem Testfenster mit.
    ``anchored=True`` (verankert): das Trainingsfenster beginnt immer am ersten Balken
    und wächst mit jedem Schritt -- klassisches "expanding window".

    ``step_bars``: wie weit jedes Fenster gegenüber dem vorigen vorrückt. Ohne Angabe
    gleich ``test_bars`` -- dann überlappen sich die Out-of-Sample-Abschnitte der
    Fenster nie.
    """

    n_windows: int
    train_bars: int
    test_bars: int
    step_bars: int | None = None
    anchored: bool = False

    def __post_init__(self) -> None:
        if self.n_windows < MIN_WINDOWS:
            raise ValueError(
                f"Walk-Forward braucht mindestens {MIN_WINDOWS} Fenster (HED-9 "
                f"Abschnitt 7, P5), angefragt: {self.n_windows}."
            )
        if self.train_bars <= 0 or self.test_bars <= 0:
            raise ValueError("train_bars und test_bars müssen positiv sein.")
        if self.step_bars is not None and self.step_bars <= 0:
            raise ValueError("step_bars muss positiv sein, wenn gesetzt.")

    @property
    def effective_step(self) -> int:
        return self.step_bars if self.step_bars is not None else self.test_bars

    def required_bars(self) -> int:
        """Wie viele Balken die Zeitreihe mindestens haben muss, um alle Fenster zu füllen."""
        return self.train_bars + self.test_bars + self.effective_step * (self.n_windows - 1)


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    index: int
    train: slice
    test: slice


def build_windows(n_bars: int, spec: WindowSpec) -> list[WalkForwardWindow]:
    """Positions-Slices (nicht Datums-Slices) für jedes Fenster, vorwärts über die Reihe.

    Reine Geometrie, kein Blick in die Daten selbst -- deshalb mit einer einfachen
    Balkenanzahl testbar, ohne eine echte Kursreihe zu brauchen.
    """
    required = spec.required_bars()
    if n_bars < required:
        raise InsufficientHistory(
            f"Walk-Forward braucht mindestens {required} Balken für {spec.n_windows} "
            f"Fenster (train={spec.train_bars}, test={spec.test_bars}, "
            f"step={spec.effective_step}); vorhanden: {n_bars}."
        )
    step = spec.effective_step
    windows = []
    for i in range(spec.n_windows):
        if spec.anchored:
            train_start = 0
            train_end = spec.train_bars + i * step
        else:
            train_start = i * step
            train_end = train_start + spec.train_bars
        test_start = train_end
        test_end = test_start + spec.test_bars
        windows.append(
            WalkForwardWindow(
                index=i, train=slice(train_start, train_end), test=slice(test_start, test_end)
            )
        )
    return windows


@dataclass(frozen=True, slots=True)
class WindowResult:
    """Out-of-Sample-Kennzahlen eines einzelnen Fensters.

    ``stats`` ist die vollständige Rückgabe von ``backtesting.Backtest.run()`` (eine
    pandas-Series) -- für alles, was über die drei im Auftrag genannten Mindestgrößen
    (P&L, Trefferquote, Anzahl Trades) hinausgeht.
    """

    window: WalkForwardWindow
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    pnl: float
    win_rate_pct: float
    trade_count: int
    stats: pd.Series = field(repr=False)


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    symbol: str
    spec: WindowSpec
    windows: list[WindowResult]

    @property
    def total_pnl(self) -> float:
        return sum(w.pnl for w in self.windows)

    @property
    def total_trades(self) -> int:
        return sum(w.trade_count for w in self.windows)

    @property
    def mean_win_rate_pct(self) -> float:
        """Mittlere Trefferquote über alle Fenster mit mindestens einem Trade.

        Ein Fenster ohne Trade liefert ``NaN`` als Trefferquote (siehe
        ``run_walk_forward``) und geht hier nicht in den Durchschnitt ein -- sonst
        würde ein handlungsloses Fenster die Quote künstlich verwässern oder, bei
        naiver NaN-Behandlung, den gesamten Durchschnitt zu NaN machen.
        """
        rates = [w.win_rate_pct for w in self.windows if w.trade_count > 0]
        return sum(rates) / len(rates) if rates else float("nan")

    def summary(self) -> dict[str, float | int]:
        """Zusammenfassende Kennzahl über alle Fenster, zusätzlich zu den Einzelwerten
        je Fenster (laut Auftrag: "zusätzlich eine zusammenfassende Kennzahl")."""
        return {
            "n_windows": len(self.windows),
            "total_pnl": self.total_pnl,
            "total_trades": self.total_trades,
            "mean_win_rate_pct": self.mean_win_rate_pct,
            "windows_with_a_trade": sum(1 for w in self.windows if w.trade_count > 0),
        }


def run_walk_forward(
    frame: pd.DataFrame,
    strategy_cls: type[Strategy],
    cost_model: BacktestCostModel,
    spec: WindowSpec,
    *,
    cash: float = 10_000.0,
    fit: Callable[[pd.DataFrame], dict[str, Any]] | None = None,
) -> WalkForwardResult:
    """Führt die Strategie über jedes Fenster von ``spec`` aus, Kosten aus ``cost_model``
    auf jeden Fill.

    ``fit``, wenn gesetzt, bekommt die Trainings-Teilreihe eines Fensters und liefert
    die Strategie-Parameter (als Keyword-Argumente für ``Backtest.run(**params)``) für
    die anschließende Out-of-Sample-Ausführung auf der Test-Teilreihe desselben
    Fensters. Dieses Modul ruft ``fit`` nur auf -- was darin passiert (Parameter
    anpassen, ein Modell trainieren) ist Quant-Researcher-Arbeit, keine, die hier
    gebaut wird. Ohne ``fit`` laufen die Klassen-Standardparameter der Strategie --
    das reicht für die rein technische Abnahme dieses Werkzeugs.
    """
    windows = build_windows(len(frame), spec)
    results = []
    for w in windows:
        train_df = frame.iloc[w.train]
        test_df = frame.iloc[w.test]
        params = fit(train_df) if fit is not None else {}
        bt = Backtest(
            test_df,
            strategy_cls,
            cash=cash,
            commission=cost_model.commission,
            spread=cost_model.backtesting_spread_param,
            # Ein am Fensterende noch offener Trade wird geschlossen und gezählt,
            # statt stillschweigend aus Trade-Anzahl/Trefferquote/P&L dieses Fensters
            # zu fallen -- sonst hinge die Kennzahl eines Fensters vom Zufall ab, ob
            # die letzte Position gerade am Fensterrand offen war.
            finalize_trades=True,
        )
        stats = bt.run(**params)
        trade_count = int(stats["# Trades"])
        win_rate = float(stats["Win Rate [%]"]) if trade_count > 0 else float("nan")
        results.append(
            WindowResult(
                window=w,
                train_start=train_df.index[0],
                train_end=train_df.index[-1],
                test_start=test_df.index[0],
                test_end=test_df.index[-1],
                pnl=float(stats["Equity Final [$]"]) - cash,
                win_rate_pct=win_rate,
                trade_count=trade_count,
                stats=stats,
            )
        )
    return WalkForwardResult(symbol=cost_model.symbol, spec=spec, windows=results)
