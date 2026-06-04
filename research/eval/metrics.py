"""Minimal P1 metric set (design §6.5 lists the full P3 suite).

Float analytics under the design-D8 exemption. P1 covers the three numbers an
operator needs to read a daily spine report: total return, CAGR, and max
drawdown. P3 extends this module with Sharpe, Sortino, Calmar, volatility drag,
risk-of-ruin, CVaR, margin-call frequency, and peak leverage.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from research.eval.results import BacktestResult

_DAYS_PER_YEAR = 365.25
#: Below this span, annualizing is statistically meaningless (a +20% move over a
#: few days annualizes to absurd numbers). CAGR returns NaN so the report shows
#: "n/a" rather than a number that reads as a bug.
_MIN_CAGR_YEARS = 0.5


def max_drawdown_pct(equity_curve: npt.NDArray[np.float64]) -> float:
    """Largest peak-to-trough decline as a positive fraction (``0.2`` = -20%).

    LIMITATION: once equity has gone non-positive (a wipeout), a percentage
    drawdown is undefined, so further collapse below zero is understated here.
    That only matters under leverage — P3 replaces this with proper
    liquidation-aware ruin metrics (absolute-dollar drawdown + margin-call
    accounting); at P1 (no leverage) equity cannot go negative on a long.
    """
    if equity_curve.size < 2:
        return 0.0
    running_max = np.maximum.accumulate(equity_curve)
    # Guard against non-positive equity (a wipeout): clamp denominator.
    safe_peak = np.where(running_max > 0.0, running_max, np.nan)
    drawdown = (equity_curve - running_max) / safe_peak
    worst = np.nanmin(drawdown)
    # ``abs`` normalizes the magnitude (drawdowns are ≤ 0) and avoids -0.0.
    return float(abs(worst)) if np.isfinite(worst) else 0.0


def cagr(result: BacktestResult) -> float:
    """Compound annual growth rate from the equity curve and date span.

    Returns NaN when the window is shorter than :data:`_MIN_CAGR_YEARS` or the
    equity is non-positive — annualizing those is meaningless, so the report
    renders "n/a" instead of an absurd number.
    """
    years = (result.dates[-1] - result.dates[0]).days / _DAYS_PER_YEAR
    start = result.starting_cash
    final = result.final_equity
    if years < _MIN_CAGR_YEARS or start <= 0.0 or final <= 0.0:
        return float("nan")
    return float((final / start) ** (1.0 / years) - 1.0)


def summarize(result: BacktestResult) -> dict[str, float]:
    """Headline metrics for the report (all floats)."""
    return {
        "total_return": result.total_return,
        "cagr": cagr(result),
        "max_drawdown_pct": max_drawdown_pct(result.equity_curve),
        "pnl": result.pnl,
        "final_equity": result.final_equity,
    }
