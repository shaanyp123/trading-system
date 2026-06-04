"""Per-bar gross leverage + the hard-cap clamp (design §6.1).

``leverage = gross_notional / equity`` where ``gross_notional = |contracts| x
multiplier x price`` (multiplier from ``research.data.contract_specs``). Tracked
per bar and reduced to ``(peak, mean, terminal)`` for the report.

Two distinct uses, kept separate:

* :func:`cap_contracts_to_leverage` — enforced at SIZING time so a position is
  never *opened* above ``leverage.cap``. The sizing schemes call this; it is the
  hard guardrail (design §6.1: "each runs under a hard ``leverage.cap``").
* :func:`compute_leverage_series` — REALIZED leverage along an equity path. As
  price moves against a held position, equity shrinks and realized leverage drifts
  *above* the entry cap — that drift toward a wipeout is part of the ruin story we
  surface, not a bug (design §6.2). Once equity is non-positive the position is
  un-leverageable (you are liquidated); leverage is ``NaN`` there, and the path is
  flagged ``wiped``.

Float analytics (design D8); the multiplier is taken as a float at the boundary
(the ``Decimal`` spec lives in ``contract_specs``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


def max_contracts_for_leverage(equity: float, price: float, multiplier: float, cap: float) -> int:
    """Largest whole contract count whose notional keeps leverage ``<= cap``.

    ``floor(cap x equity / (multiplier x price))``. Returns ``0`` for non-positive
    equity / price / cap (nothing can be held inside the cap) — never negative.
    """
    if equity <= 0.0 or price <= 0.0 or multiplier <= 0.0 or cap <= 0.0:
        return 0
    per_contract_notional = multiplier * price
    return math.floor(cap * equity / per_contract_notional)


def cap_contracts_to_leverage(
    contracts: int, *, equity: float, price: float, multiplier: float, cap: float
) -> int:
    """Clamp a signed contract count so opening it never exceeds ``cap`` leverage.

    Preserves sign; shrinks magnitude to :func:`max_contracts_for_leverage`. This
    is the HARD cap every sizing scheme runs under (design §6.1) — it can only
    reduce a position, never enlarge one.
    """
    if contracts == 0:
        return 0
    sign = 1 if contracts > 0 else -1
    capped = min(abs(contracts), max_contracts_for_leverage(equity, price, multiplier, cap))
    return sign * capped


@dataclass(frozen=True, slots=True)
class LeverageSeries:
    """Realized gross leverage along an equity path + its headline reductions.

    ``leverage[t]`` is ``NaN`` on bars where equity is non-positive (a wiped
    account cannot carry leverage). ``peak`` / ``mean`` ignore those ``NaN``s;
    ``wiped`` records whether the path ever hit non-positive equity, and
    ``first_wipe_index`` is the first such bar (``-1`` if never).
    """

    leverage: npt.NDArray[np.float64]
    peak: float
    mean: float
    terminal: float
    wiped: bool
    first_wipe_index: int

    @property
    def peak_index(self) -> int:
        """Bar index of peak realized leverage (``-1`` if the series is all-NaN)."""
        if self.leverage.size == 0 or np.all(np.isnan(self.leverage)):
            return -1
        return int(np.nanargmax(self.leverage))


def compute_leverage_series(
    positions: npt.NDArray[np.int64],
    prices: npt.NDArray[np.float64],
    equity_curve: npt.NDArray[np.float64],
    multiplier: float,
) -> LeverageSeries:
    """Realized per-bar gross leverage from positions, prices, and the equity path.

    ``leverage[t] = |positions[t]| x multiplier x prices[t] / equity_curve[t]``,
    measured mark-to-market at each bar's close. Equity ``<= 0`` ⇒ ``NaN`` (and the
    path is ``wiped``). All three inputs must share length.
    """
    n = int(positions.shape[0])
    if prices.shape[0] != n or equity_curve.shape[0] != n:
        raise ValueError(
            f"length mismatch: positions={n}, prices={prices.shape[0]}, "
            f"equity={equity_curve.shape[0]}"
        )
    if n == 0:
        empty = np.empty(0, dtype=np.float64)
        return LeverageSeries(empty, 0.0, 0.0, 0.0, wiped=False, first_wipe_index=-1)

    gross_notional = np.abs(positions).astype(np.float64) * float(multiplier) * prices
    positive_equity = equity_curve > 0.0
    leverage = np.full(n, np.nan, dtype=np.float64)
    np.divide(gross_notional, equity_curve, out=leverage, where=positive_equity)

    wipe_idx = np.flatnonzero(~positive_equity)
    wiped = bool(wipe_idx.size > 0)
    first_wipe_index = int(wipe_idx[0]) if wiped else -1

    if np.all(np.isnan(leverage)):
        peak = mean = 0.0
    else:
        peak = float(np.nanmax(leverage))
        mean = float(np.nanmean(leverage))
    terminal = float(leverage[-1]) if not math.isnan(leverage[-1]) else float("nan")

    return LeverageSeries(
        leverage=leverage,
        peak=peak,
        mean=mean,
        terminal=terminal,
        wiped=wiped,
        first_wipe_index=first_wipe_index,
    )
