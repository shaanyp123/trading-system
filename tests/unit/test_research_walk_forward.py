"""Walk-forward windowing + scoring (research/eval/walk_forward.py)."""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import numpy as np

from research.data.bars import BarSeries
from research.eval.walk_forward import add_months, generate_windows, run_walk_forward
from research.strategy.buy_and_hold import BuyAndHold
from research.strategy.donchian import DonchianBreakout


def _series(n: int = 1200, symbol: str = "TLT", start: date = date(2020, 1, 1)) -> BarSeries:
    dates = tuple(start + timedelta(days=i) for i in range(n))
    t = np.arange(n, dtype=np.float64)
    close = 100.0 + 10.0 * np.sin(t / 40.0) + 0.01 * t  # oscillating uptrend (ups + downs)
    return BarSeries(
        symbol=symbol,
        dates=dates,
        open=close.copy(),
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=np.full(n, 1_000_000.0),
    )


def test_add_months_basic() -> None:
    assert add_months(date(2024, 1, 15), 1) == date(2024, 2, 15)
    assert add_months(date(2024, 1, 15), 12) == date(2025, 1, 15)
    assert add_months(date(2024, 6, 30), 6) == date(2024, 12, 30)


def test_add_months_clamps_to_month_end() -> None:
    # Jan 31 + 1mo → Feb 29 (leap year 2024), not an invalid Feb 31.
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2024, 12, 31), 2) == date(2025, 2, 28)


def test_generate_windows_rolling_non_overlapping_oos() -> None:
    series = _series(n=1200)  # ~3.3 years of calendar days
    windows = generate_windows(series.dates, is_months=12, oos_months=6, step_months=6)
    assert len(windows) >= 2
    for w in windows:
        assert w.is_end == w.oos_start  # OOS begins exactly where IS ends
        assert w.is_bars > 0 and w.oos_bars > 0
        assert w.oos_slice[1] <= len(series)
    # step == oos_months ⇒ contiguous, non-overlapping OOS spans.
    for prev, nxt in itertools.pairwise(windows):
        assert nxt.oos_start >= prev.oos_end


def test_generate_windows_too_short_returns_empty() -> None:
    short = _series(n=60)  # ~2 months
    assert generate_windows(short.dates, is_months=24, oos_months=6) == []


def test_run_walk_forward_scores_folds() -> None:
    series = _series(n=1200)
    windows = generate_windows(series.dates, is_months=12, oos_months=6, step_months=6)
    report = run_walk_forward(series, BuyAndHold(1), windows, multiplier=1.0)
    assert report.n_folds == len(windows)
    assert np.isfinite(report.is_score)
    assert np.isfinite(report.oos_score)
    # degradation is defined (is - oos)
    assert report.degradation == report.is_score - report.oos_score


def test_run_walk_forward_donchian_runs() -> None:
    series = _series(n=1200)
    windows = generate_windows(series.dates, is_months=12, oos_months=6, step_months=6)
    report = run_walk_forward(
        series, DonchianBreakout(channel=20, contracts=1), windows, multiplier=1.0
    )
    assert report.n_folds == len(windows)
    assert len(report.folds) == len(windows)
