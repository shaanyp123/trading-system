"""Rolling in-sample / out-of-sample walk-forward windows (design §7, P4).

The anti-overfitting spine: split the daily history into rolling (IS, OOS) windows
and judge a strategy on the OOS slices it was NOT tuned on. Higher frequency = more
ways to fool yourself, so OOS is the only honest scoreboard.

**Causal-slice approach.** A research strategy is causal (``target_positions[t]``
uses bars ``[0..t]`` only — contract.py), so we run it ONCE over the full series and
slice the resulting equity curve into windows. This is both efficient (one pass per
candidate, not one per window) and correct: the strategy enters each OOS window with
the warmed-up state it really would have had, with no boundary-flattening artifact.
Per-window metrics are computed from the equity sub-curve (Sharpe / return / max-DD
of the bars inside the window).

Float analytics (design D8). Months are calendar months (operator-legible config);
window→bar mapping uses the series' actual trading dates.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import numpy.typing as npt
import structlog

from research.data.bars import BarSeries
from research.risk.metrics import sharpe_ratio
from research.screen.daily_eval import evaluate_daily
from research.strategy.contract import ResearchStrategy

_log = structlog.get_logger(__name__)

#: A window metric maps an equity sub-curve to a scalar score (higher = better).
#: Default = annualized Sharpe (risk-adjusted — the honest thing to rank on).
MetricFn = Callable[[npt.NDArray[np.float64]], float]

#: An OOS window needs at least this many bars for a metric to mean anything.
_MIN_OOS_BARS = 5
#: An IS window needs at least this many bars (enough to warm up + measure).
_MIN_IS_BARS = 20


def add_months(d: date, months: int) -> date:
    """Return ``d`` shifted by ``months`` calendar months (day clamped to month end)."""
    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1  # divmod gives a 0-based month; restore 1-based
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    """One rolling fold: an in-sample span followed by the out-of-sample span.

    Index spans are half-open ``[lo, hi)`` into the series arrays. ``is_end`` and
    ``oos_start`` coincide (OOS begins exactly where IS ends).
    """

    index: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    is_slice: tuple[int, int]
    oos_slice: tuple[int, int]

    @property
    def is_bars(self) -> int:
        return self.is_slice[1] - self.is_slice[0]

    @property
    def oos_bars(self) -> int:
        return self.oos_slice[1] - self.oos_slice[0]


def generate_windows(
    dates: tuple[date, ...],
    *,
    is_months: int,
    oos_months: int,
    step_months: int | None = None,
) -> list[WalkForwardWindow]:
    """Rolling (IS, OOS) windows over ``dates`` (ascending trading dates).

    ``step_months`` (default ``oos_months``) advances the anchor between folds; the
    default gives contiguous, non-overlapping OOS coverage (true walk-forward).
    Degenerate folds (an IS or OOS span thinner than the minimum bar counts) are
    skipped, so a short history yields fewer — or zero — windows rather than noise.
    """
    if is_months < 1 or oos_months < 1:
        raise ValueError(f"is_months/oos_months must be >= 1; got {is_months}/{oos_months}")
    step = step_months if step_months is not None else oos_months
    if step < 1:
        raise ValueError(f"step_months must be >= 1; got {step}")
    if not dates:
        return []

    def idx_ge(d: date) -> int:
        return bisect.bisect_left(dates, d)

    last = dates[-1]
    windows: list[WalkForwardWindow] = []
    anchor = dates[0]
    fold = 0
    while True:
        is_start = anchor
        is_end = add_months(is_start, is_months)
        oos_start = is_end
        oos_end = add_months(oos_start, oos_months)
        if oos_start > last:
            break
        is_lo, is_hi = idx_ge(is_start), idx_ge(is_end)
        oos_lo, oos_hi = idx_ge(oos_start), idx_ge(oos_end)
        if (is_hi - is_lo) >= _MIN_IS_BARS and (oos_hi - oos_lo) >= _MIN_OOS_BARS:
            windows.append(
                WalkForwardWindow(
                    index=fold,
                    is_start=is_start,
                    is_end=is_end,
                    oos_start=oos_start,
                    oos_end=oos_end,
                    is_slice=(is_lo, is_hi),
                    oos_slice=(oos_lo, oos_hi),
                )
            )
            fold += 1
        anchor = add_months(anchor, step)
        if anchor >= last:
            break
    return windows


@dataclass(frozen=True, slots=True)
class FoldScore:
    """A strategy's in-sample vs out-of-sample score on one fold."""

    window: WalkForwardWindow
    is_score: float
    oos_score: float


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """A strategy's walk-forward result: per-fold scores + aggregates.

    ``oos_score`` (mean OOS metric across folds) is the headline — rank on this.
    ``degradation`` = ``is_score - oos_score`` makes overfitting visible (a big drop
    from IS to OOS is the warning).
    """

    strategy_name: str
    folds: tuple[FoldScore, ...]
    is_score: float
    oos_score: float

    @property
    def degradation(self) -> float:
        return self.is_score - self.oos_score

    @property
    def n_folds(self) -> int:
        return len(self.folds)


def _nanmean(values: list[float]) -> float:
    arr = np.asarray(values, dtype=np.float64)
    finite = arr[np.isfinite(arr)]
    return float(np.mean(finite)) if finite.size else float("nan")


def run_walk_forward(
    series: BarSeries,
    strategy: ResearchStrategy,
    windows: list[WalkForwardWindow],
    *,
    multiplier: float,
    starting_cash: float = 100_000.0,
    metric_fn: MetricFn = sharpe_ratio,
) -> WalkForwardReport:
    """Score ``strategy`` across ``windows`` by slicing one full-series equity curve.

    Runs the strategy ONCE over the full series (causal), then for each window scores
    the IS and OOS equity sub-curves with ``metric_fn``. Aggregates are nan-robust
    means across folds (a fold whose sub-curve is too flat to score contributes NaN
    and is dropped from the mean, never silently counted as zero).
    """
    full = evaluate_daily(series, strategy, multiplier=multiplier, starting_cash=starting_cash)
    equity = full.equity_curve
    folds: list[FoldScore] = []
    for window in windows:
        is_lo, is_hi = window.is_slice
        oos_lo, oos_hi = window.oos_slice
        folds.append(
            FoldScore(
                window=window,
                is_score=metric_fn(equity[is_lo:is_hi]),
                oos_score=metric_fn(equity[oos_lo:oos_hi]),
            )
        )
    report = WalkForwardReport(
        strategy_name=strategy.name,
        folds=tuple(folds),
        is_score=_nanmean([f.is_score for f in folds]),
        oos_score=_nanmean([f.oos_score for f in folds]),
    )
    _log.info(
        "research_walk_forward_scored",
        strategy=strategy.name,
        folds=report.n_folds,
        is_score=round(report.is_score, 4) if np.isfinite(report.is_score) else None,
        oos_score=round(report.oos_score, 4) if np.isfinite(report.oos_score) else None,
    )
    return report
