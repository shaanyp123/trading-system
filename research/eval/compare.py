"""Normalize a candidate against benchmarks for apples-to-apples reading (§7, P4).

Every candidate is shown beside **buy-and-hold** (per contract) and, when supplied,
the **live V1** adapter — same series, same starting cash, same fill convention — so
"good" is always relative to the do-nothing alternative and the strategy already in
production. A candidate that can't beat buy-and-hold on a risk-adjusted basis is not
an edge, however pretty its own curve.

Full-sample comparison on the numpy screen (fast, non-authoritative — LEAN is the
authority for fills, design D1). Float analytics (design D8).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from research.data.bars import BarSeries
from research.eval.sweep import Candidate
from research.risk.metrics import cagr, max_drawdown_pct, sharpe_ratio
from research.screen.daily_eval import evaluate_daily

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    """One strategy's normalized full-sample line in a comparison table."""

    label: str
    role: str  # "candidate" | "benchmark"
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown_pct: float
    final_equity: float


def _row(
    label: str,
    role: str,
    series: BarSeries,
    candidate: Candidate,
    *,
    multiplier: float,
    starting_cash: float,
) -> ComparisonRow:
    result = evaluate_daily(
        series, candidate.strategy, multiplier=multiplier, starting_cash=starting_cash
    )
    return ComparisonRow(
        label=label,
        role=role,
        total_return=result.total_return,
        cagr=cagr(result),
        sharpe=sharpe_ratio(result.equity_curve),
        max_drawdown_pct=max_drawdown_pct(result.equity_curve),
        final_equity=result.final_equity,
    )


def run_compare(
    series: BarSeries,
    candidate: Candidate,
    benchmarks: list[Candidate],
    *,
    multiplier: float,
    starting_cash: float = 100_000.0,
) -> tuple[ComparisonRow, ...]:
    """Evaluate ``candidate`` and each benchmark over the full series, normalized.

    Returns the candidate row first, then one row per benchmark — all on the SAME
    ``series`` / ``starting_cash`` / fill, so the columns are directly comparable.
    """
    rows = [
        _row(
            candidate.label,
            "candidate",
            series,
            candidate,
            multiplier=multiplier,
            starting_cash=starting_cash,
        )
    ]
    for benchmark in benchmarks:
        rows.append(
            _row(
                benchmark.label,
                "benchmark",
                series,
                benchmark,
                multiplier=multiplier,
                starting_cash=starting_cash,
            )
        )
    _log.info(
        "research_compare_complete",
        candidate=candidate.label,
        benchmarks=[b.label for b in benchmarks],
        rows=len(rows),
    )
    return tuple(rows)
