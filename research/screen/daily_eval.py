"""Minimal numpy daily mark-to-market evaluator (research-only, NON-authoritative).

Close-to-close MTM with a one-bar entry lag (look-ahead-safe, design §5.5): the
position decided at ``close[t]`` earns P&L only from ``t`` onward. This is the
standard vectorized convention and is deliberately simple — it exists for fast
triage and as the buy-and-hold cross-check, NOT as a competing engine. Precise
next-open fill-price accounting, fees, slippage, margin, and liquidation are
LEAN's job and arrive with the LEAN driver in P2 (design D1).
"""

from __future__ import annotations

import numpy as np

from research.data.bars import BarSeries
from research.eval.results import BacktestResult
from research.strategy.contract import ResearchStrategy

_DEFAULT_STARTING_CASH = 100_000.0


def evaluate_daily(
    series: BarSeries,
    strategy: ResearchStrategy,
    *,
    multiplier: float,
    starting_cash: float = _DEFAULT_STARTING_CASH,
    fill: str = "close",
) -> BacktestResult:
    """Run ``strategy`` over ``series`` and return a :class:`BacktestResult`.

    ``fill="close"`` is the only modeled convention in P1 (decided at
    ``close[t]``, held with a one-bar lag). ``fill="next_open"`` raises — precise
    fill modeling is LEAN's responsibility (P2), and the toy evaluator must not
    pretend to do it.
    """
    if fill != "close":
        raise NotImplementedError(
            f"fill={fill!r} not modeled in the P1 daily evaluator; next-open / "
            "fee / slippage / margin fidelity is LEAN's job (P2 driver)"
        )
    n = len(series)
    targets = strategy.target_positions(series)
    if targets.shape != (n,):
        raise ValueError(
            f"{strategy.name}.target_positions returned shape {targets.shape}, expected ({n},)"
        )
    targets = targets.astype(np.int64, copy=False)

    # Position held over the close-to-close interval ending at bar t is the
    # target decided at t-1 (one-bar lag). Bar 0 has no prior close ⇒ flat.
    positions = np.empty(n, dtype=np.int64)
    positions[0] = 0
    if n >= 2:
        positions[1:] = targets[:-1]

    step_pnl = np.zeros(n, dtype=np.float64)
    if n >= 2:
        step_pnl[1:] = positions[1:].astype(np.float64) * multiplier * np.diff(series.close)
    equity_curve = starting_cash + np.cumsum(step_pnl)

    return BacktestResult(
        symbol=series.symbol,
        dates=series.dates,
        equity_curve=equity_curve,
        positions=positions,
        starting_cash=starting_cash,
        multiplier=multiplier,
        fill=fill,
        strategy_name=strategy.name,
    )
