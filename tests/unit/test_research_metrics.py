"""§6.5 risk + ruin metric suite (moved P1 headline → research.risk.metrics + P3)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from research.eval.results import BacktestResult
from research.risk.metrics import (
    annualized_volatility,
    cagr,
    compute_risk_metrics,
    conditional_var,
    is_wiped,
    kelly_leverage,
    longest_drawdown_days,
    max_drawdown_dollars,
    max_drawdown_pct,
    prob_drawdown_exceeds,
    sharpe_ratio,
    summarize,
    volatility_drag,
    worst_day,
)


def _result(
    equity: list[float], dates: list[date], positions: list[int] | None = None
) -> BacktestResult:
    return BacktestResult(
        symbol="X",
        dates=tuple(dates),
        equity_curve=np.asarray(equity, dtype=np.float64),
        positions=(
            np.asarray(positions, dtype=np.int64)
            if positions is not None
            else np.zeros(len(equity), dtype=np.int64)
        ),
        starting_cash=equity[0],
        multiplier=1.0,
        fill="close",
        strategy_name="t",
    )


def _year(equity: list[float], positions: list[int] | None = None) -> BacktestResult:
    """A result whose first/last dates span ~1y (so CAGR/annualization are defined)."""
    n = len(equity)
    dates = [
        date(2024, 1, 1) + (date(2025, 1, 1) - date(2024, 1, 1)) * i // (n - 1) for i in range(n)
    ]
    return _result(equity, dates, positions)


# --- P1 headline (moved here) ------------------------------------------------
def test_max_drawdown_peak_to_trough() -> None:
    assert max_drawdown_pct(np.array([100.0, 110.0, 105.0, 120.0])) == pytest.approx(5 / 110)


def test_max_drawdown_monotonic_is_zero() -> None:
    assert max_drawdown_pct(np.array([100.0, 110.0, 120.0])) == 0.0


def test_max_drawdown_wipeout_is_total() -> None:
    assert max_drawdown_pct(np.array([100.0, 50.0, 0.0])) == pytest.approx(1.0)


def test_cagr_positive_when_up() -> None:
    res = _result([100.0, 200.0], [date(2024, 1, 1), date(2025, 1, 1)])
    assert cagr(res) == pytest.approx(1.0, abs=0.01)


def test_summarize_keys() -> None:
    res = _result([100.0, 120.0], [date(2024, 1, 1), date(2025, 1, 1)])
    assert set(summarize(res)) == {
        "total_return",
        "cagr",
        "max_drawdown_pct",
        "pnl",
        "final_equity",
    }


# --- liquidation-aware drawdown (replaces the P1 post-wipeout limitation) -----
def test_max_drawdown_dollars_peak_to_trough() -> None:
    # running max [100,110,110,120]; trough vs peak = 110-105 = 5.
    assert max_drawdown_dollars(np.array([100.0, 110.0, 105.0, 120.0])) == pytest.approx(5.0)


def test_max_drawdown_dollars_holds_through_wipeout() -> None:
    # Pct DD saturates at 100% but dollars keeps counting below zero (leverage).
    curve = np.array([100.0, 50.0, -30.0])
    assert max_drawdown_dollars(curve) == pytest.approx(130.0)
    assert is_wiped(curve) is True


# --- vol / risk-adjusted -----------------------------------------------------
def test_vol_zero_for_constant_return() -> None:
    assert annualized_volatility(np.array([100.0, 110.0, 121.0])) == pytest.approx(0.0)


def test_vol_scales_with_swing() -> None:
    v = annualized_volatility(np.array([100.0, 110.0, 99.0]))  # returns +10% / -10%
    expected = float(np.std([0.1, -0.1], ddof=1) * math.sqrt(252))  # sample std of ±10%
    assert v == pytest.approx(expected, rel=1e-6)


def test_sharpe_sign_tracks_drift() -> None:
    assert sharpe_ratio(np.array([100.0, 101.0, 99.0, 100.0, 98.0])) < 0  # net down
    assert sharpe_ratio(np.array([100.0, 101.0, 102.0, 103.5, 105.0])) > 0


# --- volatility drag (the leverage tax) --------------------------------------
def test_volatility_drag_zero_for_constant_return() -> None:
    assert volatility_drag(np.array([100.0, 110.0, 121.0])) == pytest.approx(0.0, abs=1e-9)


def test_volatility_drag_positive_when_volatile() -> None:
    assert volatility_drag(np.array([100.0, 120.0, 100.0, 120.0, 100.0])) > 0.0


# --- ruin probabilities ------------------------------------------------------
def test_prob_drawdown_certain_on_negative_drift() -> None:
    curve = np.array([100.0, 99.0, 98.0, 97.0, 96.0, 95.0], dtype=np.float64)
    assert prob_drawdown_exceeds(curve, 0.5) == 1.0


def test_prob_drawdown_below_one_on_strong_positive_drift() -> None:
    curve = np.array([100.0, 102.0, 104.0, 106.0, 108.0, 110.0], dtype=np.float64)
    p = prob_drawdown_exceeds(curve, 0.5)
    assert 0.0 <= p < 1.0


def test_prob_drawdown_threshold_range_validated() -> None:
    with pytest.raises(ValueError):
        prob_drawdown_exceeds(np.array([100.0, 101.0, 102.0]), 1.5)


def test_kelly_sign_tracks_edge() -> None:
    assert kelly_leverage(np.array([100.0, 101.0, 102.0, 103.0, 104.0])) > 0
    assert kelly_leverage(np.array([100.0, 99.0, 98.0, 97.0, 96.0])) < 0


def test_fractional_kelly_rebaselined_to_asset() -> None:
    # Asset returns with analytically known full-Kelly f* = mu/sigma². The SIZED
    # path holds a constant 2x (per-bar returns exactly doubled), so the curve-
    # implied Kelly is f*/2; the mean-leverage re-baseline must report the ASSET's
    # f* and fractional_kelly ≈ 2/f* (the un-rebaselined bug reported ≈ 4/f*).
    r_asset = np.tile([0.02, 0.02, -0.01], 40)
    f_star = float(np.mean(r_asset) / np.var(r_asset, ddof=1))
    equity = 100_000.0 * np.cumprod(np.concatenate([[1.0], 1.0 + 2.0 * r_asset]))
    metrics = compute_risk_metrics(
        _year([float(v) for v in equity]), peak_leverage=2.0, mean_leverage=2.0
    )
    assert metrics.kelly_leverage == pytest.approx(f_star, rel=0.15)
    assert metrics.fractional_kelly == pytest.approx(2.0 / f_star, rel=0.15)


def test_fractional_kelly_nan_without_mean_leverage() -> None:
    # No mean leverage ⇒ no honest re-baseline to the asset ⇒ NaN, not a guess.
    metrics = compute_risk_metrics(_year([100.0, 110.0, 105.0, 120.0]), peak_leverage=2.0)
    assert math.isnan(metrics.kelly_leverage)
    assert math.isnan(metrics.fractional_kelly)


# --- drawdown shape ----------------------------------------------------------
def test_longest_drawdown_days_underwater_span() -> None:
    # Peak at index 0 (2024-01-01); never recovers 100 → underwater to the last date.
    res = _result([100.0, 95.0, 90.0], [date(2024, 1, 1), date(2024, 1, 11), date(2024, 1, 21)])
    assert longest_drawdown_days(res) == 20


def test_longest_drawdown_days_runs_to_recovery_bar() -> None:
    # Peak Jan-1, trough Jan-11, RECOVERED Jan-21: the drawdown ends at the
    # recovery bar → 20 days (stopping at the last underwater bar undercounts: 10).
    res = _result([100.0, 90.0, 105.0], [date(2024, 1, 1), date(2024, 1, 11), date(2024, 1, 21)])
    assert longest_drawdown_days(res) == 20
    # A new-highs-only curve still reports 0 (recovery bars without a drawdown).
    up = _result([100.0, 110.0, 120.0], [date(2024, 1, 1), date(2024, 1, 11), date(2024, 1, 21)])
    assert longest_drawdown_days(up) == 0


def test_worst_day_is_most_negative_return() -> None:
    assert worst_day(np.array([100.0, 110.0, 88.0])) == pytest.approx(-0.2)


def test_cvar_is_mean_of_tail() -> None:
    # 5%-quantile tail of a small sample is the single worst return.
    curve = np.array([100.0, 101.0, 102.0, 80.0, 81.0], dtype=np.float64)
    assert conditional_var(curve, 0.05) < 0.0


# --- aggregate ---------------------------------------------------------------
def test_compute_risk_metrics_report_dict_complete() -> None:
    res = _year([100.0, 110.0, 105.0, 120.0, 118.0])
    metrics = compute_risk_metrics(res, peak_leverage=2.0, mean_leverage=1.5)
    d = metrics.to_report_dict()
    for key in (
        "cagr",
        "annualized_vol",
        "sharpe",
        "sortino",
        "max_drawdown_pct",
        "max_drawdown_dollars",
        "volatility_drag",
        "p_drawdown_gt_50pct",
        "risk_of_ruin",
        "peak_leverage",
        "fractional_kelly",
    ):
        assert key in d
    assert d["peak_leverage"] == pytest.approx(2.0)


def test_compute_risk_metrics_liquidated_forces_ruin_one() -> None:
    res = _year([100.0, 60.0, 30.0, 10.0, 5.0])
    metrics = compute_risk_metrics(
        res,
        peak_leverage=8.0,
        n_margin_events=4,
        liquidated=True,
        first_liquidation_date=date(2024, 4, 1),
    )
    assert metrics.liquidated is True
    assert metrics.risk_of_ruin == 1.0
    assert metrics.first_liquidation_date == date(2024, 4, 1)
    # All-zero positions vector ⇒ the all-bars fallback denominator (5).
    assert metrics.margin_call_frequency == pytest.approx(4 / 5)


def test_margin_call_frequency_uses_position_bars() -> None:
    # 2 of 5 bars carry a position; 1 margin event ⇒ 1/2 per POSITION-BAR (the
    # denominator liquidation's summary uses), not 1/5 over all bars.
    res = _year([100.0, 99.0, 98.0, 99.0, 99.5], positions=[0, 1, 1, 0, 0])
    metrics = compute_risk_metrics(res, n_margin_events=1)
    assert metrics.margin_call_frequency == pytest.approx(0.5)


def test_compute_risk_metrics_wipeout_is_liquidated() -> None:
    res = _year([100.0, 40.0, -20.0, -50.0, -80.0])
    metrics = compute_risk_metrics(res)  # no explicit liquidated flag
    assert metrics.liquidated is True  # is_wiped() drives it
