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
from datetime import date, datetime
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

#: Vintage of the static reference schedule above (the 2022-01 schedule the LEAN
#: bundle carries) — the staleness warning compares it against the run window.
STATIC_MARGIN_VINTAGE: Final[date] = date(2022, 1, 1)

#: Reg-T cash-equity maintenance fractions for the bond ETFs: 25% of position
#: value for a LONG, 30% for a SHORT (short-equity maintenance is higher).
DEFAULT_ETF_MAINTENANCE_FRACTION: Final[Decimal] = Decimal("0.25")
DEFAULT_ETF_SHORT_MAINTENANCE_FRACTION: Final[Decimal] = Decimal("0.30")

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

    Futures use a fixed ``$/contract`` (``maintenance_per_contract``, side-
    symmetric); ETFs use a Reg-T fraction of position value — 25% long / 30%
    short. Exactly one branch applies per symbol (set by :func:`margin_model_for`).
    """

    symbol: str
    is_future: bool
    maintenance_per_contract: Decimal  # futures: fixed $/contract; ETFs: unused (0)
    etf_maintenance_fraction: Decimal  # ETFs: fraction of notional (long); futures: unused
    etf_short_maintenance_fraction: Decimal = DEFAULT_ETF_SHORT_MAINTENANCE_FRACTION

    def maintenance_per_contract_at(
        self, price: float, multiplier: float, position: int = 1
    ) -> float:
        """Maintenance margin for ONE contract at ``price`` (float, design D8).

        ``position``'s SIGN selects the ETF fraction (Reg-T: 25% long, 30% short);
        futures margin is side-symmetric, so the sign is ignored there.
        """
        if self.is_future:
            return float(self.maintenance_per_contract)
        fraction = (
            self.etf_short_maintenance_fraction if position < 0 else self.etf_maintenance_fraction
        )
        return float(fraction) * price * multiplier


def _load_lean_margin_last_row(
    data_root: Path, spec: ContractSpec
) -> tuple[Decimal, date | None] | None:
    """Last (maintenance, row-date vintage) from LEAN's margin CSV, or ``None``.

    ``future/<market_dir>/margins/<ROOT>.csv`` has ``date,initial,maintenance`` rows;
    we take the most recent. Returns ``None`` for ETFs (no fixed per-contract
    margin) or when the file is absent (the gitignored snapshot is not present in
    CI) — the caller falls back to :data:`FUTURES_MAINTENANCE_MARGIN_USD`.
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
        maintenance = Decimal(parts[2])
    except (ValueError, ArithmeticError):
        return None
    try:
        vintage: date | None = datetime.strptime(parts[0], "%Y%m%d").date()
    except ValueError:
        vintage = None  # vintage is advisory (staleness warning); the margin still loads
    return maintenance, vintage


def load_lean_maintenance_margin(data_root: Path, spec: ContractSpec) -> Decimal | None:
    """Last-row maintenance margin from LEAN's per-symbol margin CSV, or ``None``."""
    row = _load_lean_margin_last_row(data_root, spec)
    return row[0] if row is not None else None


def margin_model_for(
    spec: ContractSpec,
    *,
    data_root: Path | None = None,
    maintenance_override: Decimal | None = None,
    etf_maintenance_fraction: Decimal = DEFAULT_ETF_MAINTENANCE_FRACTION,
    window_start: date | None = None,
) -> MarginModel:
    """Build the :class:`MarginModel` for ``spec`` (override > LEAN CSV > reference).

    For futures the per-contract maintenance margin resolves in priority order:
    explicit ``maintenance_override`` → LEAN margin CSV under ``data_root`` →
    :data:`FUTURES_MAINTENANCE_MARGIN_USD` reference. ETFs always use the Reg-T
    fractions of notional (25% long / 30% short). ``window_start`` (the run's first
    session) arms a staleness check: a schedule whose vintage (last CSV row date,
    or :data:`STATIC_MARGIN_VINTAGE` for the reference dict) predates the window
    logs ``research_margin_schedule_stale`` — warn-only, margin numbers unchanged.
    """
    is_future = spec.asset_class == "future"
    if not is_future:
        return MarginModel(spec.symbol, False, Decimal("0"), etf_maintenance_fraction)
    maint = maintenance_override
    vintage: date | None = None  # an explicit override carries no schedule vintage
    if maint is None and data_root is not None:
        row = _load_lean_margin_last_row(data_root, spec)
        if row is not None:
            maint, vintage = row
    if maint is None:
        maint = FUTURES_MAINTENANCE_MARGIN_USD.get(spec.symbol)
        vintage = STATIC_MARGIN_VINTAGE if maint is not None else None
    if maint is None:
        raise KeyError(
            f"no maintenance margin for future {spec.symbol!r}: pass maintenance_override, "
            f"provide a margin CSV under data_root, or extend FUTURES_MAINTENANCE_MARGIN_USD"
        )
    if window_start is not None and vintage is not None and vintage < window_start:
        _log.warning(
            "research_margin_schedule_stale",
            symbol=spec.symbol,
            vintage=vintage.isoformat(),
            window_start=window_start.isoformat(),
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
    #: Bars with a non-zero position — the denominator ``summary()`` reports (and
    #: the one ``RiskMetrics.margin_call_frequency`` divides by).
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
            return f"no intrabar maintenance-margin breach over {self.bars_evaluated} position-bars"
        first = self.flags[0]
        return (
            f"WARNING (estimate): {self.n_flagged}/{self.bars_evaluated} position-bars would "
            f"have breached maintenance margin intraday; first {first.flag_date} "
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
            adverse_price, multiplier, position=pos
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
