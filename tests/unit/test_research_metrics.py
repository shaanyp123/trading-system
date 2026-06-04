"""Minimal P1 metric set (design §6.5; full suite is P3)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from research.eval.metrics import cagr, max_drawdown_pct, summarize
from research.eval.results import BacktestResult


def _result(equity: list[float], dates: list[date]) -> BacktestResult:
    return BacktestResult(
        symbol="X",
        dates=tuple(dates),
        equity_curve=np.asarray(equity, dtype=np.float64),
        positions=np.zeros(len(equity), dtype=np.int64),
        starting_cash=equity[0],
        multiplier=1.0,
        fill="close",
        strategy_name="t",
    )


def test_max_drawdown_peak_to_trough() -> None:
    # 100 → 110 → 105 → 120: worst draw is 110 → 105.
    assert max_drawdown_pct(np.array([100.0, 110.0, 105.0, 120.0])) == pytest.approx(5 / 110)


def test_max_drawdown_monotonic_is_zero() -> None:
    assert max_drawdown_pct(np.array([100.0, 110.0, 120.0])) == 0.0


def test_max_drawdown_wipeout_is_total() -> None:
    # Equity to zero is a 100% drawdown — the curve the leverage report must show.
    assert max_drawdown_pct(np.array([100.0, 50.0, 0.0])) == pytest.approx(1.0)


def test_cagr_zero_when_flat() -> None:
    res = _result([100.0, 100.0], [date(2024, 1, 1), date(2025, 1, 1)])
    assert cagr(res) == 0.0


def test_cagr_positive_when_up() -> None:
    res = _result([100.0, 200.0], [date(2024, 1, 1), date(2025, 1, 1)])
    # ~1 year, doubled ⇒ ~+100%.
    assert cagr(res) == pytest.approx(1.0, abs=0.01)


def test_cagr_nan_on_short_window() -> None:
    # Annualizing a few days is meaningless ⇒ NaN (report renders "n/a").
    res = _result([100.0, 120.0], [date(2024, 1, 2), date(2024, 1, 5)])
    assert math.isnan(cagr(res))


def test_summarize_keys() -> None:
    res = _result([100.0, 120.0], [date(2024, 1, 1), date(2025, 1, 1)])
    s = summarize(res)
    assert set(s) == {"total_return", "cagr", "max_drawdown_pct", "pnl", "final_equity"}
    assert s["total_return"] == pytest.approx(0.2)
