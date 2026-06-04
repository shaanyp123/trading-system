"""Daily-bar intrabar liquidation ESTIMATOR (design §6.3).

At ``Resolution.DAILY`` LEAN can only check margin at the daily bar boundary — a
maintenance-margin breach that happened and recovered *within* a day is invisible
to a close-to-close equity curve. This module overlays each bar's **high/low** on
the held position to flag "equity would have crossed maintenance margin intraday on
these N days" as a **WARNING with explicit residual uncertainty**.

It is a **flag-for-confirmation, never a verdict** (§6.3): a daily bar does not
reveal the intrabar PATH (the low and the other adverse moves may not have
coincided; equity may have recovered before any forced sale). The honest
resolution is to re-run the flagged window at minute resolution — **P5, DEFERRED**
per the 2026-06-03 sign-off. We REPORT the uncertainty; we never pretend daily bars
settle it.

Margin reference: maintenance margin per contract is money-adjacent and kept
``Decimal`` (:data:`FUTURES_MAINTENANCE_MARGIN_USD`, sourced from LEAN's per-symbol
margin DB — ``future/<market_dir>/margins/<SYMBOL>.csv``; that data is gitignored,
so a static reference is the default and is overridable per run). The per-bar
arithmetic is float (design D8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import structlog

from research.data.bars import BarSeries
from research.data.contract_specs import ContractSpec
from research.eval.results import BacktestResult

_log = structlog.get_logger(__name__)

#: Maintenance margin per contract (USD), sourced from LEAN's symbol margin DB
#: (``research/data/cache/lean_bars/future/<market_dir>/margins/<SYMBOL>.csv``, last
#: row). That snapshot is gitignored, so these are the committed reference defaults
#: (CME micro maintenance as of the 2022-01 schedule the bundle carries). They are
#: OVERRIDABLE per run; :func:`load_lean_maintenance_margin` reads the live CSV when
#: a data snapshot is present. Money-adjacent ⇒ ``Decimal`` (NOT float-exempt).
FUTURES_MAINTENANCE_MARGIN_USD: Final[dict[str, Decimal]] = {
    "/MES": Decimal("1080"),
    "/MNQ": Decimal("1500"),
    "/MYM": Decimal("1100"),  # no bundle CSV; conservative reference (≈ /MES tier)
    "/M2K": Decimal("550"),
    "/MGC": Decimal("600"),
    "/MBT": Decimal("136"),
}

#: Reg-T cash-equity maintenance fraction for the bond ETFs (25% of position value).
DEFAULT_ETF_MAINTENANCE_FRACTION: Final[Decimal] = Decimal("0.25")

_RESIDUAL_UNCERTAINTY: Final[str] = (
    "ESTIMATE, not a verdict: a daily bar hides the intrabar PATH — the high/low "
    "may not have coincided with the rest of the adverse move, and equity may have "
    "recovered before any forced sale. Confirm or deny each flagged day by re-running "
    "the window at MINUTE resolution (design §6.3 → P5, currently DEFERRED). Tick "
    "reduces but never fully eliminates the residual."
)


@dataclass(frozen=True, slots=True)
class MarginModel:
    """How to derive maintenance margin per contract for one symbol.

    Futures use a fixed ``$/contract`` (``maintenance_per_contract``); ETFs use a
    fraction of position value (``etf_maintenance_fraction x price x multiplier``).
    Exactly one branch applies per symbol (set by :func:`margin_model_for`).
    """

    symbol: str
    is_future: bool
    maintenance_per_contract: Decimal  # futures: fixed $/contract; ETFs: unused (0)
    etf_maintenance_fraction: Decimal  # ETFs: fraction of notional; futures: unused

    def maintenance_per_contract_at(self, price: float, multiplier: float) -> float:
        """Maintenance margin for ONE contract at ``price`` (float, design D8)."""
        if self.is_future:
            return float(self.maintenance_per_contract)
        return float(self.etf_maintenance_fraction) * price * multiplier


def load_lean_maintenance_margin(data_root: Path, spec: ContractSpec) -> Decimal | None:
    """Last-row maintenance margin from LEAN's per-symbol margin CSV, or ``None``.

    ``future/<market_dir>/margins/<ROOT>.csv`` has ``date,initial,maintenance`` rows;
    we take the most recent ``maintenance``. Returns ``None`` for ETFs (no fixed
    per-contract margin) or when the file is absent (the gitignored snapshot is not
    present in CI) — the caller falls back to :data:`FUTURES_MAINTENANCE_MARGIN_USD`.
    """
    if spec.asset_class != "future":
        return None
    root = spec.symbol.lstrip("/").upper()
    csv_path = data_root / "future" / spec.market_dir / "margins" / f"{root}.csv"
    if not csv_path.is_file():
        return None
    last: str | None = None
    for line in csv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("date"):
            continue
        last = line
    if last is None:
        return None
    parts = last.split(",")
    if len(parts) < 3:
        return None
    try:
        return Decimal(parts[2])
    except (ValueError, ArithmeticError):
        return None


def margin_model_for(
    spec: ContractSpec,
    *,
    data_root: Path | None = None,
    maintenance_override: Decimal | None = None,
    etf_maintenance_fraction: Decimal = DEFAULT_ETF_MAINTENANCE_FRACTION,
) -> MarginModel:
    """Build the :class:`MarginModel` for ``spec`` (override > LEAN CSV > reference).

    For futures the per-contract maintenance margin resolves in priority order:
    explicit ``maintenance_override`` → LEAN margin CSV under ``data_root`` →
    :data:`FUTURES_MAINTENANCE_MARGIN_USD` reference. ETFs always use the Reg-T
    ``etf_maintenance_fraction`` of notional.
    """
    is_future = spec.asset_class == "future"
    if not is_future:
        return MarginModel(spec.symbol, False, Decimal("0"), etf_maintenance_fraction)
    maint = maintenance_override
    if maint is None and data_root is not None:
        maint = load_lean_maintenance_margin(data_root, spec)
    if maint is None:
        maint = FUTURES_MAINTENANCE_MARGIN_USD.get(spec.symbol)
    if maint is None:
        raise KeyError(
            f"no maintenance margin for future {spec.symbol!r}: pass maintenance_override, "
            f"provide a margin CSV under data_root, or extend FUTURES_MAINTENANCE_MARGIN_USD"
        )
    return MarginModel(spec.symbol, True, maint, etf_maintenance_fraction)


@dataclass(frozen=True, slots=True)
class LiquidationFlag:
    """One bar whose intrabar adverse extreme would have breached maintenance margin."""

    index: int
    flag_date: date
    position: int  # signed contracts held over the bar
    adverse_price: float  # bar low (long) / high (short)
    equity_at_adverse: float  # MTM equity at that extreme
    maintenance_required: float  # |position| x maintenance-per-contract
    shortfall: float  # maintenance_required - equity_at_adverse (> 0)


@dataclass(frozen=True, slots=True)
class LiquidationEstimate:
    """Estimator output: flagged days + the mandatory residual-uncertainty caveat."""

    flags: tuple[LiquidationFlag, ...]
    bars_evaluated: int
    residual_uncertainty: str = _RESIDUAL_UNCERTAINTY

    @property
    def n_flagged(self) -> int:
        return len(self.flags)

    @property
    def liquidated(self) -> bool:
        """Any flag ⇒ the path would (estimate) have hit a forced liquidation."""
        return bool(self.flags)

    @property
    def first_flag(self) -> LiquidationFlag | None:
        return self.flags[0] if self.flags else None

    def summary(self) -> str:
        if not self.flags:
            return f"no intrabar maintenance-margin breach over {self.bars_evaluated} bars"
        first = self.flags[0]
        return (
            f"WARNING (estimate): {self.n_flagged}/{self.bars_evaluated} bars would have "
            f"breached maintenance margin intraday; first {first.flag_date} "
            f"(equity {first.equity_at_adverse:,.0f} < maint {first.maintenance_required:,.0f})"
        )


def estimate_intrabar_liquidation(
    result: BacktestResult,
    series: BarSeries,
    margin: MarginModel,
) -> LiquidationEstimate:
    """Flag bars whose intrabar high/low would have breached maintenance margin.

    For the position ``result.positions[t]`` held over ``(t-1, t]``, the adverse
    intrabar extreme is the bar LOW for a long / HIGH for a short. Mark-to-market
    equity at that extreme is ``equity[t-1] + position x multiplier x (adverse -
    close[t-1])``; a breach is ``equity_at_adverse < |position| x maintenance``. The
    result is a WARNING — see :data:`_RESIDUAL_UNCERTAINTY`.
    """
    positions = result.positions
    equity = result.equity_curve
    close = series.close
    n = len(series)
    if positions.shape[0] != n or equity.shape[0] != n:
        raise ValueError(
            f"length mismatch: series={n}, positions={positions.shape[0]}, equity={equity.shape[0]}"
        )
    multiplier = result.multiplier
    flags: list[LiquidationFlag] = []
    evaluated = 0
    for t in range(1, n):
        pos = int(positions[t])
        if pos == 0:
            continue
        evaluated += 1
        adverse_price = float(series.low[t]) if pos > 0 else float(series.high[t])
        equity_at_adverse = float(equity[t - 1]) + pos * multiplier * (
            adverse_price - float(close[t - 1])
        )
        maintenance_required = abs(pos) * margin.maintenance_per_contract_at(
            adverse_price, multiplier
        )
        if equity_at_adverse < maintenance_required:
            flags.append(
                LiquidationFlag(
                    index=t,
                    flag_date=series.dates[t],
                    position=pos,
                    adverse_price=adverse_price,
                    equity_at_adverse=equity_at_adverse,
                    maintenance_required=maintenance_required,
                    shortfall=maintenance_required - equity_at_adverse,
                )
            )
    estimate = LiquidationEstimate(flags=tuple(flags), bars_evaluated=evaluated)
    _log.info(
        "research_liquidation_estimate",
        symbol=series.symbol,
        bars_evaluated=evaluated,
        flagged=estimate.n_flagged,
        liquidated=estimate.liquidated,
    )
    return estimate
