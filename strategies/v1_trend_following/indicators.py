"""Indicator primitives used by V1.

Pure functions, no IO, no class state. Each takes a `BarSeries` (or a slice of
its closes) and returns a `Decimal` (or a small struct of Decimals).

Implementations are deliberately stdlib-only. The strategy runs both inside
QuantConnect LEAN (where pandas/numpy are available) and inside our own backend
backtester (where we want minimal dependencies). Routing both through the same
pure functions guarantees byte-identical signals across both runtimes — which
is exactly what the weekly LEAN-vs-vbt parity gate (backend-spec §10.2) tests.

If profiling later shows any of these is hot enough to warrant a numpy port,
benchmark the change against the parity gate's tolerance budget (<=5 bps per-trade
slippage delta) before merging.

Anti-pattern A05: prices are `Decimal` at the API boundary. Internal scalar
math may use `float` for log/sqrt/regression; results are converted back to
`Decimal` at the boundary. This is acceptable because:
- the loss is bounded (`Decimal("...").quantize(Decimal("0.00000001"))` is
  enforced at boundaries)
- the indicators are inputs to discrete decisions (compare > threshold), not to
  P&L bookkeeping where rounding accumulates
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Final, NamedTuple

from strategies.v1_trend_following.signals import Bar

_PRICE_QUANTUM: Final[Decimal] = Decimal("0.00000001")


class DonchianChannel(NamedTuple):
    """Highest high and lowest low over the lookback window, both inclusive."""

    high: Decimal
    low: Decimal


def donchian_channel(bars: Sequence[Bar], lookback_days: int) -> DonchianChannel:
    """Donchian channel over the `lookback_days` bars PRIOR TO the current bar.

    Per backend-spec §2.3 ("`LOOKBACK_DAYS_DONCHIAN`-day high broken to upside,
    low broken to downside"), the channel is the high/low of the N PRIOR days
    — exclusive of the current (last) bar. The caller compares the current
    bar's close against this prior-window channel to detect a breakout.

    An inclusive-of-current-bar window would make a long breakout
    mathematically impossible: it would require `current_close > max(highs)`
    which includes the current bar's high, and `current_close <= current_high`
    by definition. The implementation here matches the spec language + the
    canonical Turtle Donchian interpretation.

    Requires `len(bars) >= lookback_days + 1` (lookback for the channel +
    the current bar). Raises `ValueError` otherwise.
    """
    if lookback_days < 2:
        raise ValueError(f"lookback_days={lookback_days} must be >= 2")
    if len(bars) < lookback_days + 1:
        raise ValueError(
            f"donchian_channel: need {lookback_days + 1} bars (lookback + current), got {len(bars)}"
        )
    # Window = the lookback_days bars PRIOR to the current (last) bar.
    window = bars[-(lookback_days + 1) : -1]
    high = max(b.high for b in window)
    low = min(b.low for b in window)
    return DonchianChannel(high=high, low=low)


def simple_moving_average(closes: Sequence[Decimal], window: int) -> Decimal:
    """Arithmetic mean of the trailing `window` closes."""
    if window < 1:
        raise ValueError(f"window={window} must be >= 1")
    if len(closes) < window:
        raise ValueError(f"sma: need {window} closes, got {len(closes)}")
    tail = closes[-window:]
    total = sum(tail, Decimal(0))
    return (total / Decimal(window)).quantize(_PRICE_QUANTUM)


def average_true_range(bars: Sequence[Bar], lookback_days: int) -> Decimal:
    """Wilder-style ATR over the trailing `lookback_days` bars.

    True range for bar i = max(
        high_i - low_i,
        |high_i - close_{i-1}|,
        |low_i  - close_{i-1}|,
    )
    ATR = arithmetic mean of TRs over the window. (Wilder's original recursion
    is equivalent in the limit; the arithmetic mean over a fixed window matches
    QC's `IndicatorExtensions.SMA(rangeBars, n)` and vectorbt's `vbt.ATR`. We
    use it here for parity.)
    """
    if lookback_days < 2:
        raise ValueError(f"lookback_days={lookback_days} must be >= 2")
    if len(bars) < lookback_days + 1:
        raise ValueError(
            f"atr: need {lookback_days + 1} bars (lookback + prior close), got {len(bars)}"
        )
    window = bars[-(lookback_days + 1) :]
    trs: list[Decimal] = []
    prev_close = window[0].close
    for b in window[1:]:
        tr = max(
            b.high - b.low,
            abs(b.high - prev_close),
            abs(b.low - prev_close),
        )
        trs.append(tr)
        prev_close = b.close
    total = sum(trs, Decimal(0))
    return (total / Decimal(len(trs))).quantize(_PRICE_QUANTUM)


def hurst_exponent_rs(
    closes: Sequence[Decimal],
    min_chunks: int = 4,
    min_chunk_size: int = 4,
) -> Decimal:
    """Hurst exponent via rescaled-range (R/S) analysis.

    Method:
      1. Convert closes to log-returns r_i = ln(P_i / P_{i-1}).
      2. For each chunk-size n in {N//min_chunks, halving down to min_chunk_size},
         partition the return series into floor(N/n) non-overlapping chunks.
      3. For each chunk: mean-adjust returns; cumulative deviation Z; range R =
         max(Z) - min(Z); std S; record R/S.
      4. Average R/S across chunks -> (R/S)_n.
      5. Linear regression of ln((R/S)_n) on ln(n). Slope = Hurst exponent H.

    Conventions:
      - H ~= 0.50 -> random walk (white noise)
      - H > 0.50 -> persistent / trending (positive autocorrelation)
      - H < 0.50 -> mean-reverting

    Returns Decimal quantized to 4 decimal places. Raises `ValueError` for
    series too short (< 40 closes) or pathological (zero variance / degenerate
    regression with fewer than two valid chunk sizes).

    NOTE: R/S has known small-sample bias (over-estimates H for short series).
    For Phase 1 we accept this — the threshold is configurable and the strategy
    is tuned around it. Phase 2 may swap in DFA (detrended fluctuation analysis)
    if the calibration evidence supports it; that's a strategy logic change
    requiring a PR with risk-review-approved.

    Defaults of `min_chunks=4` and `min_chunk_size=4` are tuned so that a
    60-close series (the V1 LOOKBACK_DAYS_DONCHIAN default) yields two valid
    chunk sizes — the minimum needed for a regression slope. Longer series
    yield more points and a more stable estimate.
    """
    n_closes = len(closes)
    if n_closes < 40:
        raise ValueError(f"hurst_rs: need >=40 closes for stable estimate, got {n_closes}")
    if min_chunks < 2:
        raise ValueError(f"min_chunks={min_chunks} must be >= 2")
    if min_chunk_size < 2:
        raise ValueError(f"min_chunk_size={min_chunk_size} must be >= 2")

    # Log returns. Convert Decimal -> float for log/std/regression.
    returns: list[float] = []
    for i in range(1, n_closes):
        prev = float(closes[i - 1])
        curr = float(closes[i])
        if prev <= 0 or curr <= 0:
            raise ValueError(f"hurst_rs: non-positive close at index {i}")
        returns.append(math.log(curr / prev))
    n_returns = len(returns)

    # Chunk sizes: start at N // min_chunks, halve down until min_chunk_size.
    chunk_sizes: list[int] = []
    n = n_returns // min_chunks
    while n >= min_chunk_size:
        chunk_sizes.append(n)
        n = n // 2
    if len(chunk_sizes) < 2:
        raise ValueError(
            f"hurst_rs: insufficient series length for chunking, "
            f"n_returns={n_returns}, chunk_sizes={chunk_sizes}"
        )

    log_n: list[float] = []
    log_rs: list[float] = []
    for chunk_size in chunk_sizes:
        n_chunks = n_returns // chunk_size
        rs_values: list[float] = []
        for c in range(n_chunks):
            chunk = returns[c * chunk_size : (c + 1) * chunk_size]
            mean = sum(chunk) / chunk_size
            mean_adj = [r - mean for r in chunk]
            cum = []
            running = 0.0
            for x in mean_adj:
                running += x
                cum.append(running)
            r_range = max(cum) - min(cum)
            variance = sum(x * x for x in mean_adj) / chunk_size
            s = math.sqrt(variance)
            if s == 0 or r_range == 0:
                continue
            rs_values.append(r_range / s)
        if not rs_values:
            continue
        avg_rs = sum(rs_values) / len(rs_values)
        log_n.append(math.log(chunk_size))
        log_rs.append(math.log(avg_rs))

    if len(log_n) < 2:
        raise ValueError("hurst_rs: not enough valid chunk sizes for regression")

    # OLS slope of log_rs ~ log_n. Slope = Hurst exponent.
    n_pts = len(log_n)
    mean_x = sum(log_n) / n_pts
    mean_y = sum(log_rs) / n_pts
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(log_n, log_rs, strict=True))
    den = sum((x - mean_x) ** 2 for x in log_n)
    if den == 0:
        raise ValueError("hurst_rs: zero variance in log_n (degenerate regression)")
    slope = num / den
    return Decimal(str(round(slope, 4)))


def efficiency_ratio(closes: Sequence[Decimal]) -> Decimal:
    """Kaufman Efficiency Ratio (ER) over the provided closes.

        ER = |close[-1] - close[0]| / sum(|close[i] - close[i-1]|)

    The numerator is the *net* directional move over the window; the
    denominator is the *total path length* (sum of absolute bar-to-bar
    moves). ER lies in [0, 1]:

      - ER -> 1.0 : a clean, efficient trend — price marched in one
                    direction with little back-and-forth.
      - ER -> 0.0 : choppy / mean-reverting — lots of motion, no net
                    progress.

    Direction-agnostic: the absolute net move scores an efficient down-move
    exactly like an efficient up-move (mirroring the sign-agnostic reading
    of the Hurst gate this indicator replaces as V1 entry gate #3).

    This is the third entry gate per backend-spec §2.3: it measures
    *trend quality*. Unlike the R/S Hurst estimator it replaced, ER is
    strongly correlated with trend magnitude (sim corr ~+0.97) and is not
    biased on short windows, so it actually discriminates noise from trend
    on the 60-bar V1 lookback rather than culling at near-random.

    Anti-pattern A05: the ratio is computed in `float` (bounded to [0, 1]
    and fed only into a `>= threshold` comparison — never P&L bookkeeping),
    then converted back to `Decimal` and quantized to `_PRICE_QUANTUM` at
    the boundary, matching the other indicators in this module.

    Degenerate window — zero total path length (every close identical):
    return `Decimal(0)`. A flat series has no efficient trend, and the 0/0
    case must not raise. A single dominant gap can inflate ER toward 1.0
    (its known failure mode); the ATR-based trailing stop is the backstop,
    not this gate.

    Requires `len(closes) >= 2` (one net move + at least one path step).
    Raises `ValueError` otherwise. In the V1 pipeline this is always called
    on the `LOOKBACK_DAYS_DONCHIAN`-close slice (60 closes by default); the
    INSUFFICIENT_BAR_HISTORY guard (driven by `MA_SLOW_DAYS`, not by this
    function raising) fires long before a sub-2-close window could reach
    here. The 2-close floor is the mathematical minimum, not the operating
    point.
    """
    n = len(closes)
    if n < 2:
        raise ValueError(f"efficiency_ratio: need >=2 closes, got {n}")

    # float for the division per A05; closes are Decimal at the boundary.
    net_change = abs(float(closes[-1]) - float(closes[0]))
    path_length = 0.0
    for i in range(1, n):
        path_length += abs(float(closes[i]) - float(closes[i - 1]))

    if path_length == 0.0:
        # Flat / degenerate window: no path, no efficiency. Avoid 0/0.
        return Decimal(0)

    return Decimal(str(net_change / path_length)).quantize(_PRICE_QUANTUM)
