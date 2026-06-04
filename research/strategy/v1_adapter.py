"""V1 adapter — backtest the PRODUCTION strategy by replaying its REAL logic.

``V1Adapter`` is a :class:`~research.strategy.contract.ResearchStrategy` that
reproduces production V1's per-bar DIRECTION by calling the SAME code the live
system + LEAN run — ``strategies.v1_trend_following.V1TrendFollowing`` —
bar-by-bar over the daily series. Nothing is re-implemented, so the backtest's
entry/exit timing can never silently drift from production V1 (the design's
``v1_adapter`` "wraps strategies/v1_trend_following for apples-to-apples", §4.1).

It emits UNIT direction (``+1`` long / ``-1`` short / ``0`` flat). The harness's
sizing scheme (``vol_target`` = V1's Clenow sizing) + the leverage cap supply the
MAGNITUDE — the leverage sweep keys off ``np.sign(target_positions)`` (run.py), so
the adapter's one job is the direction/timing, which is the V1-specific part.

**Faithful (real V1 logic):**
* Entry — Donchian breakout + MA trend filter + Kaufman ER gate (``generate_signals``).
* Indicator exits — reversal / trend-flip / decommission, ``MIN_HOLDING_DAYS``-gated
  (``generate_exit_candidates``).

**Approximated (close-based; design: LEAN is the authority for fills):**
* V1's ATR protective stop is an INTRABAR bracket order (execution-layer, not in
  ``generate_exit_candidates``). Here it is a CLOSE-based check — ``close ≤ stop``
  (long) exits at that bar. A daily bar hides the intrabar path, so the exact
  trigger + fill price are LEAN's job; this is the documented numpy-screen limit.
  The position's P&L is the evaluator's close-to-close MTM, not the stop fill.

Decimal in / float out at the boundary (design D8): V1 prices are ``Decimal``; the
research ``BarSeries`` is float, so bars are converted in, and the output is an int
direction array.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import numpy.typing as npt

from research.data.bars import BarSeries
from research.strategy.contract import ResearchStrategy
from strategies.v1_trend_following.parameters import V1Parameters, default_v1_parameters
from strategies.v1_trend_following.signals import (
    Bar,
    Direction,
)
from strategies.v1_trend_following.signals import (
    BarSeries as V1BarSeries,
)
from strategies.v1_trend_following.signals import (
    Position as V1Position,
)
from strategies.v1_trend_following.strategy import V1TrendFollowing

_DIR_TO_INT = {Direction.LONG: 1, Direction.SHORT: -1, Direction.FLAT: 0}


class V1Adapter(ResearchStrategy):
    """Replay production V1's entry/exit decisions as a research strategy."""

    def __init__(self, parameters: V1Parameters | None = None) -> None:
        self._params = parameters or default_v1_parameters()
        self._v1 = V1TrendFollowing(self._params)
        self.name = "v1_adapter"

    @property
    def warmup_bars(self) -> int:
        return self._v1.min_required_bars

    def _to_v1_bars(self, series: BarSeries) -> list[Bar]:
        return [
            Bar(
                session_date=series.dates[i],
                open=Decimal(str(float(series.open[i]))),
                high=Decimal(str(float(series.high[i]))),
                low=Decimal(str(float(series.low[i]))),
                close=Decimal(str(float(series.close[i]))),
                volume=int(series.volume[i]),
            )
            for i in range(len(series))
        ]

    def target_positions(self, series: BarSeries) -> npt.NDArray[np.int64]:
        n = len(series)
        market = series.symbol
        v1_bars = self._to_v1_bars(series)
        targets = np.zeros(n, dtype=np.int64)

        direction = Direction.FLAT
        stop_price: Decimal | None = None
        avg_cost: Decimal | None = None
        opened_at: date | None = None

        min_bars = self._v1.min_required_bars
        for t in range(n):
            if t + 1 < min_bars:
                targets[t] = _DIR_TO_INT[direction]  # warming up — stay flat
                continue
            window = V1BarSeries(market=market, bars=tuple(v1_bars[: t + 1]))
            day = series.dates[t]

            if direction is Direction.FLAT:
                result = self._v1.generate_signals(
                    active_universe={market: window},
                    current_positions={},
                    as_of_session_date=day,
                )
                signal = next((s for s in result.signals if s.market == market), None)
                if signal is not None:
                    direction = signal.direction
                    stop_price = signal.stop_price
                    avg_cost = signal.decision_price
                    opened_at = day
            else:
                close_t = Decimal(str(float(series.close[t])))
                stopped = stop_price is not None and (
                    (direction is Direction.LONG and close_t <= stop_price)
                    or (direction is Direction.SHORT and close_t >= stop_price)
                )
                exited = stopped
                if not stopped:
                    position = V1Position(
                        market=market,
                        direction=direction,
                        quantity=_DIR_TO_INT[direction],
                        avg_cost=avg_cost if avg_cost is not None else close_t,
                        opened_at_session_date=opened_at,
                    )
                    exit_result = self._v1.generate_exit_candidates(
                        active_universe={market: window},
                        current_positions={market: position},
                        as_of_session_date=day,
                    )
                    exited = any(s.market == market for s in exit_result.signals)
                if exited:
                    direction = Direction.FLAT
                    stop_price = None
                    avg_cost = None
                    opened_at = None

            targets[t] = _DIR_TO_INT[direction]
        return targets
