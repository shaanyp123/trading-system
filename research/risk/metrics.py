"""The full §6.5 risk + ruin metric suite — surface ruin, don't hide it.

Moved + extended from the P1 ``research.eval.metrics`` (per the P1 README note: the
headline three move here with the leverage work). P1's ``max_drawdown_pct`` carried
a post-wipeout LIMITATION (a percentage drawdown is undefined once equity goes
non-positive under leverage); this module adds the **liquidation-aware** companions
that hold under leverage: absolute-dollar drawdown, a ``wiped``/``liquidated`` flag,
margin-call frequency, peak leverage, and parametric ruin probabilities.

Every leverage report computes and shows (design §6.5): CAGR, annualized vol,
Sharpe, Sortino, **max drawdown** (pct + dollars), Calmar/MAR, **volatility drag**
(geometric vs arithmetic), **risk-of-ruin / P(drawdown > X%)**, time-to-recovery,
worst day/week, downside deviation, CVaR / tail loss, **margin-call frequency**,
**peak leverage used**, and a **Kelly / fractional-Kelly** context line. The
deliverable must let the operator SEE the leverage at which liquidation occurs and
how often — that is the headline, not a footnote.

Float analytics (design D8). Annualization uses 252 trading days (vol / Sharpe /
Sortino, matching the live sizer) and calendar days for CAGR.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Final

import numpy as np
import numpy.typing as npt

from research.eval.results import BacktestResult

_DAYS_PER_YEAR: Final[float] = 365.25
TRADING_DAYS_PER_YEAR: Final[int] = 252
#: Below this span, annualizing is statistically meaningless (a +20% move over a
#: few days annualizes to absurd numbers). CAGR returns NaN so the report shows
#: "n/a" rather than a number that reads as a bug.
_MIN_CAGR_YEARS: Final[float] = 0.5


# --------------------------------------------------------------------------- #
# returns + the P1 headline (moved)
# --------------------------------------------------------------------------- #
def daily_returns(equity_curve: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Simple bar-over-bar returns; ``NaN`` where the prior equity is non-positive.

    Once equity is wiped out a percentage return is undefined, so those steps are
    ``NaN`` and the downstream stats use the finite subset (a wipeout is surfaced by
    the absolute-dollar drawdown + the ``liquidated`` flag, not swept under a ratio).
    """
    n = equity_curve.shape[0]
    if n < 2:
        return np.empty(0, dtype=np.float64)
    prev = equity_curve[:-1]
    out = np.full(n - 1, np.nan, dtype=np.float64)
    ok = prev > 0.0
    np.divide(equity_curve[1:] - prev, prev, out=out, where=ok)
    return out


def _finite(arr: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    return arr[np.isfinite(arr)]


def max_drawdown_pct(equity_curve: npt.NDArray[np.float64]) -> float:
    """Largest peak-to-trough decline as a positive fraction (``0.2`` = -20%).

    LIMITATION (P1): a percentage drawdown loses its clean [0, 1] meaning once equity
    goes non-positive under leverage. It stays normalized to the running PEAK, so a
    collapse past zero reads as >100% (e.g. equity at -20% of a 100k peak → 1.2). The
    number errs LARGE (it never UNDERstates), but the honest leverage headline is
    :func:`max_drawdown_dollars` + :func:`is_wiped`, which stay well-defined through a
    wipeout.
    """
    if equity_curve.size < 2:
        return 0.0
    running_max = np.maximum.accumulate(equity_curve)
    safe_peak = np.where(running_max > 0.0, running_max, np.nan)
    drawdown = (equity_curve - running_max) / safe_peak
    worst = np.nanmin(drawdown)
    return float(abs(worst)) if np.isfinite(worst) else 0.0


def max_drawdown_dollars(equity_curve: npt.NDArray[np.float64]) -> float:
    """Largest peak-to-trough decline in ABSOLUTE dollars (liquidation-aware).

    Unlike :func:`max_drawdown_pct` this is well-defined through a wipeout (the
    trough can be below zero under leverage), so it is the honest drawdown headline
    for a leveraged path.
    """
    if equity_curve.size < 2:
        return 0.0
    running_max = np.maximum.accumulate(equity_curve)
    return float(np.max(running_max - equity_curve))


def is_wiped(equity_curve: npt.NDArray[np.float64]) -> bool:
    """Did the close-to-close equity path ever hit non-positive equity?"""
    return bool(np.any(equity_curve <= 0.0))


def cagr(result: BacktestResult) -> float:
    """Compound annual growth rate from the equity curve and calendar span.

    NaN when the window is shorter than :data:`_MIN_CAGR_YEARS` or equity is
    non-positive — annualizing those is meaningless, so the report renders "n/a".
    """
    years = (result.dates[-1] - result.dates[0]).days / _DAYS_PER_YEAR
    start = result.starting_cash
    final = result.final_equity
    if years < _MIN_CAGR_YEARS or start <= 0.0 or final <= 0.0:
        return float("nan")
    return float((final / start) ** (1.0 / years) - 1.0)


# --------------------------------------------------------------------------- #
# volatility / risk-adjusted return
# --------------------------------------------------------------------------- #
def annualized_volatility(equity_curve: npt.NDArray[np.float64]) -> float:
    """Annualized realized vol of bar returns (``sqrt(252)`` scaling)."""
    r = _finite(daily_returns(equity_curve))
    if r.size < 2:
        return float("nan")
    return float(np.std(r, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def sharpe_ratio(equity_curve: npt.NDArray[np.float64], rf_annual: float = 0.0) -> float:
    """Annualized Sharpe from bar returns (excess over a flat risk-free rate)."""
    r = _finite(daily_returns(equity_curve))
    if r.size < 2:
        return float("nan")
    sd = float(np.std(r, ddof=1))
    if sd == 0.0:
        return float("nan")
    rf_per_bar = rf_annual / TRADING_DAYS_PER_YEAR
    return float((np.mean(r) - rf_per_bar) / sd * math.sqrt(TRADING_DAYS_PER_YEAR))


def downside_deviation(equity_curve: npt.NDArray[np.float64], target: float = 0.0) -> float:
    """Annualized downside deviation of bar returns below ``target`` (per bar)."""
    r = _finite(daily_returns(equity_curve))
    if r.size < 1:
        return float("nan")
    downside = np.minimum(r - target, 0.0)
    return float(math.sqrt(float(np.mean(downside**2))) * math.sqrt(TRADING_DAYS_PER_YEAR))


def sortino_ratio(equity_curve: npt.NDArray[np.float64], target_annual: float = 0.0) -> float:
    """Annualized Sortino (excess return over annualized downside deviation)."""
    r = _finite(daily_returns(equity_curve))
    if r.size < 2:
        return float("nan")
    dd = downside_deviation(equity_curve, target=target_annual / TRADING_DAYS_PER_YEAR)
    if not math.isfinite(dd) or dd == 0.0:
        return float("nan")
    mean_annual = float(np.mean(r)) * TRADING_DAYS_PER_YEAR
    return float((mean_annual - target_annual) / dd)


def calmar_ratio(result: BacktestResult) -> float:
    """CAGR / max drawdown (MAR ratio). NaN if drawdown is zero or CAGR is n/a."""
    growth = cagr(result)
    mdd = max_drawdown_pct(result.equity_curve)
    if not math.isfinite(growth) or mdd == 0.0:
        return float("nan")
    return growth / mdd


def volatility_drag(equity_curve: npt.NDArray[np.float64]) -> float:
    """Annualized gap between arithmetic and geometric mean return (the vol tax).

    ``arithmetic_mean - geometric_mean`` per bar, annualized — the return the path
    loses to volatility. Rises ~quadratically with leverage, which is why a
    leveraged equity curve can compound to less than its un-levered twin.
    """
    r = _finite(daily_returns(equity_curve))
    if r.size < 2:
        return float("nan")
    arithmetic = float(np.mean(r))
    growth = 1.0 + r
    if np.any(growth <= 0.0):  # a -100% bar ⇒ geometric mean undefined (ruin)
        return float("nan")
    geometric = float(np.exp(np.mean(np.log(growth)))) - 1.0
    return (arithmetic - geometric) * TRADING_DAYS_PER_YEAR


# --------------------------------------------------------------------------- #
# drawdown shape / tails
# --------------------------------------------------------------------------- #
def longest_drawdown_days(result: BacktestResult) -> int:
    """Longest span (calendar days) equity spent below a prior peak (time to recovery).

    If the path never recovers its peak, the span runs to the last bar. ``0`` when
    the curve only ever makes new highs.
    """
    equity = result.equity_curve
    dates = result.dates
    n = equity.shape[0]
    if n < 2:
        return 0
    peak = float(equity[0])
    peak_date = dates[0]
    longest = 0
    underwater = False
    for t in range(1, n):
        if equity[t] >= peak:
            # A drawdown ends at the RECOVERY bar — count the span up to it before
            # resetting the peak (the last underwater bar alone undercounts).
            if underwater:
                longest = max(longest, (dates[t] - peak_date).days)
                underwater = False
            peak = float(equity[t])
            peak_date = dates[t]
        else:
            underwater = True
            longest = max(longest, (dates[t] - peak_date).days)
    return longest


def worst_day(equity_curve: npt.NDArray[np.float64]) -> float:
    """Most negative single-bar return (``-0.08`` = a -8% day)."""
    r = _finite(daily_returns(equity_curve))
    return float(np.min(r)) if r.size else float("nan")


def worst_week(equity_curve: npt.NDArray[np.float64], window: int = 5) -> float:
    """Most negative trailing ``window``-bar (≈ 1 week) return."""
    n = equity_curve.shape[0]
    if n <= window:
        return float("nan")
    prev = equity_curve[:-window]
    fwd = equity_curve[window:]
    ok = prev > 0.0
    rolling = np.full(prev.shape[0], np.nan, dtype=np.float64)
    np.divide(fwd - prev, prev, out=rolling, where=ok)
    finite = _finite(rolling)
    return float(np.min(finite)) if finite.size else float("nan")


def value_at_risk(equity_curve: npt.NDArray[np.float64], q: float = 0.05) -> float:
    """Historical VaR: the ``q``-quantile bar return (a negative loss threshold)."""
    r = _finite(daily_returns(equity_curve))
    if r.size < 2:
        return float("nan")
    return float(np.quantile(r, q))


def conditional_var(equity_curve: npt.NDArray[np.float64], q: float = 0.05) -> float:
    """Expected shortfall: mean of returns at or below the ``q``-quantile (CVaR)."""
    r = _finite(daily_returns(equity_curve))
    if r.size < 2:
        return float("nan")
    threshold = float(np.quantile(r, q))
    tail = r[r <= threshold]
    return float(np.mean(tail)) if tail.size else threshold


# --------------------------------------------------------------------------- #
# ruin probabilities + Kelly (liquidation-aware)
# --------------------------------------------------------------------------- #
def prob_drawdown_exceeds(equity_curve: npt.NDArray[np.float64], threshold: float) -> float:
    """Parametric P(equity ever falls ≥ ``threshold`` below its start).

    A Brownian-drift approximation on log-returns: with per-bar log-drift ``mu`` and
    vol ``sigma``, the probability of ever reaching a log-level ``ln(1-threshold)`` below
    the start is ``exp(2 mu ln(1-threshold) / sigma²)`` for ``mu > 0`` (else ``1.0`` — a
    non-positive-drift path is ruined eventually). This is a forward-looking CONTEXT
    estimate, NOT the realized path — the realized facts are the max-drawdown +
    ``liquidated`` fields. ``threshold`` in ``(0, 1)``.
    """
    if not 0.0 < threshold < 1.0:
        raise ValueError(f"threshold must be in (0, 1); got {threshold}")
    pos = equity_curve[equity_curve > 0.0]
    if pos.size < 3:
        return float("nan")
    g = np.diff(np.log(pos))
    mu = float(np.mean(g))
    sigma = float(np.std(g, ddof=1))
    if sigma == 0.0:
        return 0.0 if mu > 0.0 else 1.0
    if mu <= 0.0:
        return 1.0
    exponent = 2.0 * mu * math.log(1.0 - threshold) / (sigma**2)
    return float(min(1.0, max(0.0, math.exp(exponent))))


def kelly_leverage(equity_curve: npt.NDArray[np.float64]) -> float:
    """Full-Kelly leverage implied by the return distribution (``mu / sigma²`` per bar).

    The growth-optimal leverage multiple for this return stream. NB: fed a SIZED
    path's curve (returns already levered ~L x the asset's), this is ≈ ``f*/L``,
    not the asset's full-Kelly ``f*`` — :func:`compute_risk_metrics` re-baselines
    by the realized mean leverage before building the fractional-Kelly context
    (design §6.5): a peak leverage above full Kelly is the over-betting regime
    where volatility drag and ruin dominate.
    """
    r = _finite(daily_returns(equity_curve))
    if r.size < 2:
        return float("nan")
    var = float(np.var(r, ddof=1))
    if var == 0.0:
        return float("nan")
    return float(np.mean(r)) / var


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RiskMetrics:
    """The full §6.5 suite for one equity path (float analytics, design D8)."""

    total_return: float
    cagr: float
    annualized_vol: float
    sharpe: float
    sortino: float
    downside_deviation: float
    max_drawdown_pct: float
    max_drawdown_dollars: float
    calmar: float
    volatility_drag: float
    longest_drawdown_days: int
    worst_day: float
    worst_week: float
    var_5pct: float
    cvar_5pct: float
    p_drawdown_gt_20pct: float
    p_drawdown_gt_50pct: float
    risk_of_ruin: float
    peak_leverage: float
    mean_leverage: float
    #: The ASSET's full-Kelly f* (curve-implied Kelly re-baselined by mean realized
    #: leverage); ``fractional_kelly`` = peak_leverage / this.
    kelly_leverage: float
    fractional_kelly: float
    #: n_margin_events / bars-with-a-position (liquidation's ``summary()`` uses the
    #: same denominator); falls back to ALL bars when no position-bar is recorded.
    margin_call_frequency: float
    n_margin_events: int
    liquidated: bool
    first_liquidation_date: date | None

    def to_report_dict(self) -> dict[str, float]:
        """Float-only view for the report's ``result.json`` (booleans/dates excluded).

        The ``liquidated`` flag + ``first_liquidation_date`` are rendered separately
        (the red banner); this dict holds the numeric metrics.
        """
        return {
            "total_return": self.total_return,
            "cagr": self.cagr,
            "annualized_vol": self.annualized_vol,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "downside_deviation": self.downside_deviation,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_dollars": self.max_drawdown_dollars,
            "calmar": self.calmar,
            "volatility_drag": self.volatility_drag,
            "longest_drawdown_days": float(self.longest_drawdown_days),
            "worst_day": self.worst_day,
            "worst_week": self.worst_week,
            "var_5pct": self.var_5pct,
            "cvar_5pct": self.cvar_5pct,
            "p_drawdown_gt_20pct": self.p_drawdown_gt_20pct,
            "p_drawdown_gt_50pct": self.p_drawdown_gt_50pct,
            "risk_of_ruin": self.risk_of_ruin,
            "peak_leverage": self.peak_leverage,
            "mean_leverage": self.mean_leverage,
            "kelly_leverage": self.kelly_leverage,
            "fractional_kelly": self.fractional_kelly,
            "margin_call_frequency": self.margin_call_frequency,
            "n_margin_events": float(self.n_margin_events),
        }


def compute_risk_metrics(
    result: BacktestResult,
    *,
    peak_leverage: float = float("nan"),
    mean_leverage: float = float("nan"),
    n_margin_events: int = 0,
    liquidated: bool = False,
    first_liquidation_date: date | None = None,
) -> RiskMetrics:
    """Assemble the full §6.5 suite for ``result``.

    Leverage stats come from :class:`research.risk.leverage.LeverageSeries`; margin /
    liquidation facts from :mod:`research.risk.liquidation` (the daily estimator) or
    parsed LEAN margin events. ``risk_of_ruin`` is ``1.0`` when the path was
    liquidated/wiped (it happened), else the parametric P(drawdown > 50%).
    """
    equity = result.equity_curve
    n_bars = max(len(result.dates), 1)
    wiped = is_wiped(equity)
    ruined = liquidated or wiped
    p_dd_50 = prob_drawdown_exceeds(equity, 0.50)
    risk_of_ruin = 1.0 if ruined else p_dd_50
    # The sized path's returns are already levered (~mean_leverage x the asset's),
    # so the curve-implied Kelly is ≈ f*/L; multiply by mean realized leverage to
    # re-baseline to the ASSET's full-Kelly f* (else fractional_kelly ≈ L²/f* —
    # wrong by a factor of L). NaN/0 mean leverage ⇒ no honest re-baseline ⇒ NaN.
    kelly_curve = kelly_leverage(equity)
    kelly_asset = (
        kelly_curve * mean_leverage
        if math.isfinite(kelly_curve) and math.isfinite(mean_leverage) and mean_leverage != 0.0
        else float("nan")
    )
    fractional_kelly = (
        peak_leverage / kelly_asset
        if math.isfinite(peak_leverage) and math.isfinite(kelly_asset) and kelly_asset != 0.0
        else float("nan")
    )
    # Margin-call frequency per POSITION-BAR (a flat bar cannot have a margin
    # event), matching liquidation's summary denominator; all-bars fallback keeps
    # the ratio defined when positions could not be reconstructed (e.g. a parsed
    # LEAN result with no fills).
    position_bars = int(np.count_nonzero(result.positions))
    margin_denominator = position_bars if position_bars > 0 else n_bars
    return RiskMetrics(
        total_return=result.total_return,
        cagr=cagr(result),
        annualized_vol=annualized_volatility(equity),
        sharpe=sharpe_ratio(equity),
        sortino=sortino_ratio(equity),
        downside_deviation=downside_deviation(equity),
        max_drawdown_pct=max_drawdown_pct(equity),
        max_drawdown_dollars=max_drawdown_dollars(equity),
        calmar=calmar_ratio(result),
        volatility_drag=volatility_drag(equity),
        longest_drawdown_days=longest_drawdown_days(result),
        worst_day=worst_day(equity),
        worst_week=worst_week(equity),
        var_5pct=value_at_risk(equity),
        cvar_5pct=conditional_var(equity),
        p_drawdown_gt_20pct=prob_drawdown_exceeds(equity, 0.20),
        p_drawdown_gt_50pct=p_dd_50,
        risk_of_ruin=risk_of_ruin,
        peak_leverage=peak_leverage,
        mean_leverage=mean_leverage,
        kelly_leverage=kelly_asset,
        fractional_kelly=fractional_kelly,
        margin_call_frequency=n_margin_events / margin_denominator,
        n_margin_events=n_margin_events,
        liquidated=ruined,
        first_liquidation_date=first_liquidation_date,
    )


def summarize(result: BacktestResult) -> dict[str, float]:
    """P1 daily-spine headline metrics (backward-compatible with the P1 report)."""
    return {
        "total_return": result.total_return,
        "cagr": cagr(result),
        "max_drawdown_pct": max_drawdown_pct(result.equity_curve),
        "pnl": result.pnl,
        "final_equity": result.final_equity,
    }
