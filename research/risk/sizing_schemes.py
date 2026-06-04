"""Position-sizing schemes for the leverage sweep (design §6.1).

Five pure sizing functions the operator selects in config — **fixed**,
**fixed-fractional**, **vol-target** (V1's Clenow-style ``VOL_TARGET_PCT_ANNUAL``),
**ATR-based** (V1's stop logic), and **risk-parity** — each producing an integer,
signed contract count under a HARD ``leverage.cap`` (enforced via
:func:`research.risk.leverage.cap_contracts_to_leverage`).

**Parity to the live authority (design §6.1).** :func:`vol_target_notional` is the
single-instrument case of ``services/risk/sizing.py`` Stage 1 (inverse-vol weight =
1 ⇒ ``leverage = vol_target x m_combined / sigma``; ``notional = leverage x
equity``). ``tests/unit/test_research_sizing_parity_pin.py`` pins it against the live
sizer's public Stage-1 vector so the two never silently diverge. We IMPORT the live
result; we never modify ``services/risk/`` (a forbidden path).

Float analytics (design D8). The multiplier crosses in as a float; the ``Decimal``
contract spec stays in ``contract_specs``. The parity pin re-materializes the
boundary comparison as ``Decimal`` (§13 / R7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

from research.data.bars import BarSeries
from research.eval.results import BacktestResult
from research.risk.leverage import (
    LeverageSeries,
    cap_contracts_to_leverage,
    compute_leverage_series,
)

#: Annualization factor for vol scaling — 252 CME trading days/yr, matching the
#: live sizer (``services/risk/sizing.py::TRADING_DAYS_PER_YEAR``) so the
#: vol-target parity holds on the same convention.
TRADING_DAYS_PER_YEAR: Final[int] = 252

SchemeName = Literal["fixed", "fixed_fractional", "vol_target", "atr", "risk_parity"]
KNOWN_SCHEMES: Final[tuple[SchemeName, ...]] = (
    "fixed",
    "fixed_fractional",
    "vol_target",
    "atr",
    "risk_parity",
)


# --------------------------------------------------------------------------- #
# the parity-pinned core: Clenow vol-target == live sizer Stage 1 (N=1)
# --------------------------------------------------------------------------- #
def vol_target_notional(
    *, equity: float, sigma_annual: float, vol_target_pct_annual: float, m_combined: float = 1.0
) -> float:
    """Target gross notional for a single instrument at a vol target (Clenow).

    ``equity x vol_target_pct_annual x m_combined / sigma_annual`` — identical to
    ``services/risk/sizing.py`` Stage 1's ``unconstrained_notional`` when the active
    set is one market (inverse-vol weight collapses to 1 and portfolio vol = the
    instrument's annual vol). Raises on non-positive ``sigma_annual`` (the live
    sizer raises ``SizingError`` there too).
    """
    if sigma_annual <= 0.0:
        raise ValueError(f"sigma_annual must be positive; got {sigma_annual}")
    return equity * vol_target_pct_annual * m_combined / sigma_annual


def risk_parity_notionals(
    *,
    equity: float,
    sigmas_annual: npt.NDArray[np.float64],
    vol_target_pct_annual: float,
) -> npt.NDArray[np.float64]:
    """Inverse-vol (risk-parity) notionals across N instruments at a portfolio target.

    Weights ``w_i ∝ 1/sigma_i`` (normalized); portfolio vol under the research
    independence approximation ``sqrt(Σ (w_i sigma_i)²)``; ``leverage = vol_target /
    portfolio_vol``; ``notional_i = leverage x w_i x equity``. For ``N = 1`` this
    equals :func:`vol_target_notional` exactly (so the single-instrument sweep's
    ``risk_parity`` and ``vol_target`` agree). The live sizer (Stage 1) uses the
    FULL covariance, not this diagonal approximation — risk-parity here is a
    research screen, NOT the authority.
    """
    if sigmas_annual.ndim != 1 or sigmas_annual.size == 0:
        raise ValueError("sigmas_annual must be a non-empty 1-D vector")
    if (sigmas_annual <= 0.0).any():
        raise ValueError("every sigma_annual must be positive")
    raw = 1.0 / sigmas_annual
    weights = raw / raw.sum()
    portfolio_vol = float(np.sqrt(np.sum((weights * sigmas_annual) ** 2)))
    if portfolio_vol <= 0.0:
        raise ValueError("degenerate portfolio vol")
    leverage = vol_target_pct_annual / portfolio_vol
    return np.asarray(leverage * weights * float(equity), dtype=np.float64)


# --------------------------------------------------------------------------- #
# per-bar sizing context + schemes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SizingContext:
    """Inputs to size ONE instrument on ONE bar (float analytics, design D8).

    ``direction`` is the strategy's sign for the bar (+1 long / -1 short / 0 flat);
    schemes return a signed contract count in that direction, clamped to
    ``leverage_cap``. ``sigma_annual`` (for vol-target / risk-parity) and ``atr``
    (for ATR sizing, in price units) are ``NaN`` during warmup ⇒ size 0.
    """

    equity: float
    price: float
    direction: int
    multiplier: float
    leverage_cap: float
    sigma_annual: float = float("nan")
    atr: float = float("nan")


@dataclass(frozen=True, slots=True)
class SizingScheme:
    """A scheme selected in config: its ``name`` + scheme-specific params."""

    name: SchemeName
    #: fixed: ``contracts``; fixed_fractional: ``fraction``; vol_target/risk_parity:
    #: ``vol_target_pct_annual`` (+ optional ``m_combined``); atr: ``risk_pct`` +
    #: ``stop_atr_mult``.
    params: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in KNOWN_SCHEMES:
            raise ValueError(f"unknown sizing scheme {self.name!r}; known: {KNOWN_SCHEMES}")


def _round_to_contracts(notional: float, price: float, multiplier: float) -> int:
    """Whole contracts whose notional is closest to ``notional`` (banker-free round)."""
    per_contract = multiplier * price
    if per_contract <= 0.0:
        return 0
    return round(notional / per_contract)


def size_for_bar(scheme: SizingScheme, ctx: SizingContext) -> int:
    """Signed integer contracts for ``ctx`` under ``scheme``, clamped to the cap.

    A flat bar (``direction == 0``), a warmup bar (``NaN`` sizing input), or a
    degenerate price returns 0. The result is always within ``leverage_cap`` —
    :func:`cap_contracts_to_leverage` can only shrink, never enlarge (design §6.1).
    """
    if ctx.direction == 0 or ctx.equity <= 0.0 or ctx.price <= 0.0:
        return 0
    p = scheme.params

    if scheme.name == "fixed":
        magnitude = abs(int(p.get("contracts", 1)))
    elif scheme.name == "fixed_fractional":
        notional = float(p["fraction"]) * ctx.equity
        magnitude = abs(_round_to_contracts(notional, ctx.price, ctx.multiplier))
    elif scheme.name in ("vol_target", "risk_parity"):
        if not np.isfinite(ctx.sigma_annual) or ctx.sigma_annual <= 0.0:
            return 0
        notional = vol_target_notional(
            equity=ctx.equity,
            sigma_annual=ctx.sigma_annual,
            vol_target_pct_annual=float(p["vol_target_pct_annual"]),
            m_combined=float(p.get("m_combined", 1.0)),
        )
        magnitude = abs(_round_to_contracts(notional, ctx.price, ctx.multiplier))
    elif scheme.name == "atr":
        if not np.isfinite(ctx.atr) or ctx.atr <= 0.0:
            return 0
        # Clenow ATR sizing: units = risk_budget / (stop_distance x multiplier).
        risk_dollars = float(p["risk_pct"]) * ctx.equity
        stop_distance = float(p["stop_atr_mult"]) * ctx.atr
        per_contract_risk = stop_distance * ctx.multiplier
        magnitude = abs(round(risk_dollars / per_contract_risk)) if per_contract_risk > 0 else 0
    else:  # pragma: no cover - guarded by SizingScheme.__post_init__
        raise ValueError(f"unhandled scheme {scheme.name!r}")

    signed = (1 if ctx.direction > 0 else -1) * magnitude
    return cap_contracts_to_leverage(
        signed,
        equity=ctx.equity,
        price=ctx.price,
        multiplier=ctx.multiplier,
        cap=ctx.leverage_cap,
    )


# --------------------------------------------------------------------------- #
# sizing inputs estimated from the bar series
# --------------------------------------------------------------------------- #
def rolling_annualized_vol(
    close: npt.NDArray[np.float64], lookback: int
) -> npt.NDArray[np.float64]:
    """Per-bar annualized realized vol from trailing simple returns (``NaN`` warmup).

    ``vol[t]`` = sample std of the ``lookback`` returns ending at ``t``, x
    ``sqrt(252)``. Bars without a full ``lookback`` of returns are ``NaN`` so the
    schemes size 0 there. This is a sweep INPUT, not the parity boundary — the pin
    feeds ``vol_target_notional`` a fixed sigma on both sides.
    """
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 2 or lookback < 2:
        return out
    returns = np.diff(close) / close[:-1]  # returns[i] is the move into bar i+1
    ann = float(np.sqrt(TRADING_DAYS_PER_YEAR))
    for t in range(lookback, n):
        window = returns[t - lookback : t]  # returns ending at bar t (exclusive of t→)
        out[t] = float(np.std(window, ddof=1)) * ann
    return out


def wilders_atr(
    high: npt.NDArray[np.float64],
    low: npt.NDArray[np.float64],
    close: npt.NDArray[np.float64],
    lookback: int,
) -> npt.NDArray[np.float64]:
    """Wilder's ATR in price units (``NaN`` until the first full window).

    True range ``= max(high-low, |high-prev_close|, |low-prev_close|)``; ATR seeds
    on the first ``lookback`` TRs (simple mean) then Wilder-smooths. Matches V1's
    ``ATR_LOOKBACK_DAYS`` stop basis (``contract_specs`` is the price scale).
    """
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 2 or lookback < 1:
        return out
    tr = np.empty(n, dtype=np.float64)
    tr[0] = float(high[0] - low[0])
    prev_close = close[:-1]
    tr[1:] = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)]
    )
    if n <= lookback:
        return out
    atr = float(np.mean(tr[1 : lookback + 1]))  # seed on TRs[1..lookback]
    out[lookback] = atr
    for t in range(lookback + 1, n):
        atr = (atr * (lookback - 1) + float(tr[t])) / lookback
        out[t] = atr
    return out


# --------------------------------------------------------------------------- #
# the sizing-aware evaluator (P3 analog of screen.daily_eval.evaluate_daily)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SizedPath:
    """A sized, leverage-capped equity path + its realized leverage series."""

    result: BacktestResult
    leverage: LeverageSeries
    sigma_annual: npt.NDArray[np.float64]
    atr: npt.NDArray[np.float64]
    leverage_cap: float
    scheme_name: str


def simulate_sized_path(
    series: BarSeries,
    directions: npt.NDArray[np.int64],
    scheme: SizingScheme,
    *,
    multiplier: float,
    starting_cash: float,
    leverage_cap: float,
    vol_lookback: int = 60,
    atr_lookback: int = 20,
) -> SizedPath:
    """Simulate a per-bar-sized position under ``leverage_cap`` → equity + leverage.

    ``directions[t]`` is the strategy's sign decided at ``close[t]``; the position
    held over ``(t, t+1]`` is sized from ``close[t]`` / prior-bar equity (one-bar
    lag, look-ahead-safe — design §5.5), capped to ``leverage_cap`` every bar. The
    cap binds at SIZING time; realized leverage can still drift above it as equity
    erodes (that drift toward ruin is what the report surfaces, design §6.2). This
    path RIDES THROUGH any wipeout (it does not auto-liquidate) — the liquidation
    estimator (``research.risk.liquidation``) overlays the intrabar margin breach,
    and the ruin metrics flag a non-positive-equity path.
    """
    n = len(series)
    if directions.shape != (n,):
        raise ValueError(f"directions shape {directions.shape} != ({n},)")
    sigma = rolling_annualized_vol(series.close, vol_lookback)
    atr = wilders_atr(series.high, series.low, series.close, atr_lookback)

    positions = np.zeros(n, dtype=np.int64)
    equity = np.empty(n, dtype=np.float64)
    equity[0] = starting_cash
    for t in range(1, n):
        ctx = SizingContext(
            equity=float(equity[t - 1]),
            price=float(series.close[t - 1]),
            direction=int(directions[t - 1]),
            multiplier=multiplier,
            leverage_cap=leverage_cap,
            sigma_annual=float(sigma[t - 1]),
            atr=float(atr[t - 1]),
        )
        positions[t] = size_for_bar(scheme, ctx)
        step_pnl = float(positions[t]) * multiplier * float(series.close[t] - series.close[t - 1])
        equity[t] = equity[t - 1] + step_pnl

    result = BacktestResult(
        symbol=series.symbol,
        dates=series.dates,
        equity_curve=equity,
        positions=positions,
        starting_cash=starting_cash,
        multiplier=multiplier,
        fill="close",
        strategy_name=f"{scheme.name}@{leverage_cap:g}x",
    )
    leverage = compute_leverage_series(positions, series.close, equity, multiplier)
    return SizedPath(
        result=result,
        leverage=leverage,
        sigma_annual=sigma,
        atr=atr,
        leverage_cap=leverage_cap,
        scheme_name=scheme.name,
    )
