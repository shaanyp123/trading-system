"""Donchian breakout reference strategy (numpy side of the §6.6 parity rail).

A deliberately simple, deterministic, ROUND-TRIPPING long-only channel breakout —
chosen for the parity rail because it opens AND closes trades (so trade-count +
per-trade-slippage parity are meaningful, unlike buy-and-hold which never exits).
The IDENTICAL rules are expressed as a LEAN ``QCAlgorithm`` in
``research/lean/projects/donchian_reference.py``; running both on the same daily
ETF series and comparing within the §6.6 tolerances is the trust-bridge proof.

Rules (long-only):
* **Enter** ``contracts`` long when ``close[t]`` breaks above the Donchian upper
  channel = ``max(high[t-channel : t])`` (the prior ``channel`` bars, excluding t).
* **Exit** to flat when ``close[t]`` breaks below the lower channel =
  ``min(low[t-channel : t])``.

**Look-ahead safety (design §5.5 / contract.py).** ``target_positions(series)[t]``
uses bars ``[0..t]`` only: the channel is built from bars strictly before ``t`` and
compared to ``close[t]``. The evaluator applies the one-bar entry lag, so a position
decided at ``t`` earns P&L only from ``t`` onward.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from research.data.bars import BarSeries
from research.strategy.contract import ResearchStrategy


class DonchianBreakout(ResearchStrategy):
    """Long-only Donchian channel breakout with a single ``channel`` lookback."""

    def __init__(self, channel: int = 20, contracts: int = 1) -> None:
        if channel < 2:
            raise ValueError(f"channel must be >= 2, got {channel}")
        if contracts <= 0:
            raise ValueError(f"contracts must be positive (long-only), got {contracts}")
        self.channel = channel
        self.contracts = contracts
        self.name = f"donchian({channel},{contracts})"

    @property
    def warmup_bars(self) -> int:
        return self.channel

    def target_positions(self, series: BarSeries) -> npt.NDArray[np.int64]:
        n = len(series)
        high = series.high
        low = series.low
        close = series.close
        targets = np.zeros(n, dtype=np.int64)
        state = 0
        for t in range(n):
            if t >= self.channel:
                upper = float(np.max(high[t - self.channel : t]))
                lower = float(np.min(low[t - self.channel : t]))
                if state == 0 and close[t] > upper:
                    state = self.contracts
                elif state != 0 and close[t] < lower:
                    state = 0
            targets[t] = state
        return targets
