"""``BacktestResult`` — the normalized output of any research evaluation.

Float-valued under the design-D8 analytics exemption. When a result feeds a
governance artifact (a PR backtest delta), the boundary code re-materializes the
headline numbers as ``Decimal``/strings — this container itself stays float for
numpy interop.

This is the SINGLE canonical result type for the harness. The P2 LEAN driver
parses raw LEAN output and NORMALIZES it into this same ``BacktestResult`` (its
``research/lean/results.py`` is a parser, not a second result type), so
``research/eval/compare.py`` can compare the numpy screen and LEAN apples-to-apples
without a translation layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Equity curve + positions for one instrument under one strategy.

    ``equity_curve[t]`` is account equity at ``dates[t]`` (``starting_cash`` plus
    cumulative mark-to-market P&L). ``positions[t]`` is the contract count held
    over the close-to-close interval ending at ``dates[t]``.
    """

    symbol: str
    dates: tuple[date, ...]
    equity_curve: npt.NDArray[np.float64]
    positions: npt.NDArray[np.int64]
    starting_cash: float
    multiplier: float
    fill: str
    strategy_name: str

    @property
    def final_equity(self) -> float:
        return float(self.equity_curve[-1])

    @property
    def pnl(self) -> float:
        return float(self.equity_curve[-1] - self.starting_cash)

    @property
    def total_return(self) -> float:
        """Cumulative return on starting cash (e.g. ``0.25`` = +25%)."""
        return float(self.equity_curve[-1] / self.starting_cash - 1.0)
