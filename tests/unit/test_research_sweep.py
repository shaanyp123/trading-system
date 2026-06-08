"""OOS-ranked sweep + multiple-testing context (research/eval/sweep.py) + compare."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

from research.data.bars import BarSeries
from research.eval.compare import run_compare
from research.eval.sweep import (
    Candidate,
    SweepReport,
    SweepRow,
    expected_max_sharpe_under_null,
    run_sweep,
)
from research.eval.walk_forward import generate_windows
from research.strategy.buy_and_hold import BuyAndHold
from research.strategy.donchian import DonchianBreakout


def _series(n: int = 1200) -> BarSeries:
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(n))
    t = np.arange(n, dtype=np.float64)
    close = 100.0 + 10.0 * np.sin(t / 40.0) + 0.01 * t
    return BarSeries(
        symbol="TLT",
        dates=dates,
        open=close.copy(),
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=np.full(n, 1_000_000.0),
    )


def test_expected_max_sharpe_under_null_monotonic_in_trials() -> None:
    n_obs = 252
    nulls = [expected_max_sharpe_under_null(k, n_obs) for k in (2, 5, 20, 100)]
    assert all(math.isfinite(x) for x in nulls)
    assert nulls == sorted(nulls)  # more trials ⇒ a higher noise bar to clear


def test_expected_max_sharpe_under_null_degenerate() -> None:
    assert math.isnan(expected_max_sharpe_under_null(1, 252))  # one trial: no MT inflation
    assert math.isnan(expected_max_sharpe_under_null(10, 1))  # no OOS data


def test_run_sweep_ranks_on_oos_descending() -> None:
    series = _series()
    windows = generate_windows(series.dates, is_months=12, oos_months=6, step_months=6)
    candidates = [
        Candidate(f"channel={c}", DonchianBreakout(channel=c, contracts=1))
        for c in (10, 20, 30, 40)
    ]
    report = run_sweep(series, candidates, windows, multiplier=1.0)
    assert report.n_trials == 4
    assert report.n_oos_obs > 0
    assert report.best is report.rows[0]
    # Rows are ranked by OOS score descending (NaN sorts last).
    finite = [r.oos_score for r in report.rows if math.isfinite(r.oos_score)]
    assert finite == sorted(finite, reverse=True)


def test_sweep_report_likely_overfit_logic() -> None:
    rows = (SweepRow(label="a", is_score=2.0, oos_score=0.10, degradation=1.9, n_folds=3),)
    # best OOS (0.10) below the null (0.50) ⇒ likely overfit.
    overfit = SweepReport(
        metric_name="sharpe", rows=rows, n_trials=50, n_oos_obs=120, expected_max_under_null=0.50
    )
    assert overfit.likely_overfit is True
    # best OOS (0.10) above a tiny null ⇒ not flagged.
    edge = SweepReport(
        metric_name="sharpe", rows=rows, n_trials=2, n_oos_obs=2000, expected_max_under_null=0.05
    )
    assert edge.likely_overfit is False
    assert edge.best is rows[0]


def test_run_compare_candidate_vs_benchmark() -> None:
    series = _series()
    candidate = Candidate("donchian(20)", DonchianBreakout(channel=20, contracts=1))
    rows = run_compare(
        series, candidate, [Candidate("buy_and_hold", BuyAndHold(1))], multiplier=1.0
    )
    assert len(rows) == 2
    assert rows[0].role == "candidate" and rows[0].label == "donchian(20)"
    assert rows[1].role == "benchmark" and rows[1].label == "buy_and_hold"
    assert all(math.isfinite(r.total_return) for r in rows)
