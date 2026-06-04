"""Buy-and-hold reference + the P1 data-plumbing exactness proof (design §8).

Buy-and-hold is the cheapest possible proof that the daily loader read the right
closes in the right order: its close-to-close P&L is hand-computable. P1
acceptance asserts that :func:`buy_and_hold_result` reproduces
``contracts * multiplier * (close[-1] - close[0])`` exactly, and that the generic
:func:`research.screen.daily_eval.evaluate_daily` agrees.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import numpy.typing as npt

from research.data.bars import BarSeries
from research.eval.results import BacktestResult
from research.strategy.contract import ResearchStrategy


class BuyAndHold(ResearchStrategy):
    """Hold a constant ``contracts`` position for the whole series."""

    def __init__(self, contracts: int = 1) -> None:
        if contracts == 0:
            raise ValueError("buy-and-hold needs a non-zero position")
        self.contracts = contracts
        self.name = f"buy_and_hold({contracts})"

    def target_positions(self, series: BarSeries) -> npt.NDArray[np.int64]:
        return np.full(len(series), self.contracts, dtype=np.int64)


def buy_and_hold_result(
    series: BarSeries,
    *,
    multiplier: Decimal,
    contracts: int = 1,
    starting_cash: float | None = None,
) -> BacktestResult:
    """Compute the canonical close-to-close buy-and-hold result DIRECTLY.

    Independent of the generic evaluator (so the two can be cross-checked).
    ``starting_cash`` defaults to the entry notional
    (``contracts * multiplier * close[0]``) so that ``total_return`` equals the
    instrument's price return ``close[-1]/close[0] - 1`` for a single contract.
    """
    mult = float(multiplier)
    close = series.close
    if starting_cash is None:
        starting_cash = abs(contracts) * mult * float(close[0])
    # Position held over each close-to-close interval; bar 0 has no prior close.
    positions = np.full(len(series), contracts, dtype=np.int64)
    positions[0] = 0
    step_pnl = np.zeros(len(series), dtype=np.float64)
    step_pnl[1:] = contracts * mult * np.diff(close)
    equity_curve = starting_cash + np.cumsum(step_pnl)
    return BacktestResult(
        symbol=series.symbol,
        dates=series.dates,
        equity_curve=equity_curve,
        positions=positions,
        starting_cash=starting_cash,
        multiplier=mult,
        fill="close",
        strategy_name=f"buy_and_hold({contracts})",
    )
