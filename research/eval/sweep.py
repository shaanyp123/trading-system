"""Parameter sweep ranked on OUT-OF-SAMPLE performance (design §7, §9 P4).

Sweeps fool you twice: the best in-sample combo is the luckiest, not the best, and
trying N combos inflates the winner's apparent edge. So this module (a) ranks every
combo on its walk-forward OOS score — never IS — and shows the IS→OOS degradation,
and (b) prints a multiple-testing reality-check: how many combos were tried and the
Sharpe a pure-noise winner would be expected to post over that many trials. A best
OOS score below that null is a flashing "probably overfit" sign.

Float analytics (design D8). The null expectation is a first-order Gumbel/Sharpe-SE
approximation (documented at :func:`expected_max_sharpe_under_null`) — a CONTEXT
haircut to temper the headline, not a precise significance test.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

import structlog

from research.data.bars import BarSeries
from research.eval.walk_forward import (
    MetricFn,
    WalkForwardWindow,
    run_walk_forward,
)
from research.risk.metrics import TRADING_DAYS_PER_YEAR, sharpe_ratio
from research.strategy.contract import ResearchStrategy

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One point in the parameter grid: a label + the built strategy."""

    label: str
    strategy: ResearchStrategy


@dataclass(frozen=True, slots=True)
class SweepRow:
    """One candidate's walk-forward result line (ranked by ``oos_score``)."""

    label: str
    is_score: float
    oos_score: float
    degradation: float
    n_folds: int


def _expected_max_standard_normal(n: int) -> float:
    """Approximate ``E[max of n iid N(0,1)]`` via Blom's order-statistic formula.

    ``Phi^-1((n - 0.375) / (n + 0.25))`` — accurate across small AND large n and
    monotonic increasing in n (the asymptotic Gumbel mean is neither at small n).
    """
    if n <= 1:
        return 0.0
    p = (n - 0.375) / (n + 0.25)
    return statistics.NormalDist().inv_cdf(p)


def expected_max_sharpe_under_null(n_trials: int, n_oos_obs: int) -> float:
    """Annualized Sharpe a pure-noise winner is expected to post over ``n_trials``.

    First-order reality-check (not a precise test): under the null (true Sharpe 0),
    a single annualized Sharpe estimated over ``n_oos_obs`` daily bars has standard
    error ≈ ``sqrt(252 / n_oos_obs)``; the best of ``n_trials`` independent such
    estimates is ≈ ``E[max of n_trials N(0,1)] x that SE``. Independence is
    optimistic (real grids are correlated, which LOWERS the true expected max), so
    this errs toward a STRICTER bar — a conservative haircut. Returns NaN when there
    is too little OOS data to say anything.
    """
    if n_trials <= 1 or n_oos_obs < 2:
        return float("nan")
    sharpe_se = math.sqrt(TRADING_DAYS_PER_YEAR / n_oos_obs)
    return _expected_max_standard_normal(n_trials) * sharpe_se


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Ranked-on-OOS sweep with multiple-testing context (design §7)."""

    metric_name: str
    rows: tuple[SweepRow, ...]  # ranked by oos_score descending (NaN last)
    n_trials: int
    n_oos_obs: int
    expected_max_under_null: float

    @property
    def best(self) -> SweepRow | None:
        return self.rows[0] if self.rows else None

    @property
    def likely_overfit(self) -> bool:
        """True when the best OOS score is within what random chance would produce."""
        best = self.best
        if best is None or not math.isfinite(best.oos_score):
            return False
        if not math.isfinite(self.expected_max_under_null):
            return False
        return best.oos_score <= self.expected_max_under_null


def _rank_key(row: SweepRow) -> float:
    # Sort by OOS score descending; push NaN to the bottom.
    return row.oos_score if math.isfinite(row.oos_score) else float("-inf")


def run_sweep(
    series: BarSeries,
    candidates: list[Candidate],
    windows: list[WalkForwardWindow],
    *,
    multiplier: float,
    starting_cash: float = 100_000.0,
    metric_fn: MetricFn = sharpe_ratio,
    metric_name: str = "sharpe",
) -> SweepReport:
    """Walk-forward every candidate and rank on aggregate OOS ``metric_fn``.

    Each candidate is scored via :func:`run_walk_forward` (full-series causal run,
    sliced into the shared ``windows``). Rows are ranked by OOS score; the
    multiple-testing null is computed from the trial count and the total OOS bars.
    """
    if not candidates:
        raise ValueError("run_sweep needs at least one candidate")
    if not windows:
        raise ValueError("run_sweep needs at least one walk-forward window")
    rows: list[SweepRow] = []
    for candidate in candidates:
        wf = run_walk_forward(
            series,
            candidate.strategy,
            windows,
            multiplier=multiplier,
            starting_cash=starting_cash,
            metric_fn=metric_fn,
        )
        rows.append(
            SweepRow(
                label=candidate.label,
                is_score=wf.is_score,
                oos_score=wf.oos_score,
                degradation=wf.degradation,
                n_folds=wf.n_folds,
            )
        )
    rows.sort(key=_rank_key, reverse=True)
    n_oos_obs = sum(w.oos_bars for w in windows)
    null = expected_max_sharpe_under_null(len(candidates), n_oos_obs)
    report = SweepReport(
        metric_name=metric_name,
        rows=tuple(rows),
        n_trials=len(candidates),
        n_oos_obs=n_oos_obs,
        expected_max_under_null=null,
    )
    _log.info(
        "research_sweep_complete",
        metric=metric_name,
        trials=report.n_trials,
        best=report.best.label if report.best else None,
        best_oos=round(report.best.oos_score, 4)
        if report.best and math.isfinite(report.best.oos_score)
        else None,
        expected_max_under_null=round(null, 4) if math.isfinite(null) else None,
        likely_overfit=report.likely_overfit,
    )
    return report
