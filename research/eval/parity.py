"""§6.6 vbt/numpy ↔ LEAN parity comparison logic (design §6.6, §8 trust bridge).

The locked criteria (dev-guide §1.5 / §6.6):

* per-trade slippage diff ``<= 5 bps``
* aggregate P&L divergence ``<= 0.5%`` of starting equity
* trade-count divergence ``<= 5%``

This module is the REUSABLE, LEAN-free core of the rail: trade extraction +
matching + the three checks are pure functions over numpy results / parsed LEAN
trades, so the parity *logic* is unit-tested without Docker/LEAN. The integration
test (``tests/integration/test_vbt_lean_parity.py``) wires real LEAN output in and
SKIPS when LEAN is absent.

**Decimal at the boundary (design D8).** Prices / P&L compare as ``Decimal``. The
numpy side's trade prices come from the close series (its close-to-close MTM
convention); the LEAN side's come from ``TotalPerformance.ClosedTrades``. The
aggregate-P&L check uses the equity-derived total P&L of each engine (robust to an
open position at the backtest end, which closed-trade sums miss).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import numpy.typing as npt

from research.eval.results import BacktestResult
from research.lean.results import LeanTrade

#: Locked §6.6 tolerances.
MAX_SLIPPAGE_BPS: Decimal = Decimal("5")
MAX_PNL_FRACTION: Decimal = Decimal("0.005")
MAX_COUNT_FRACTION: float = 0.05


@dataclass(frozen=True, slots=True)
class Trade:
    """A normalized round-trip for comparison. ``exit_price=None`` ⇒ still open."""

    entry_index: int  # bar index (numpy side) or -1 (lean side)
    entry_price: Decimal
    exit_price: Decimal | None
    quantity: int  # absolute contract count
    direction: int  # +1 long, -1 short
    pnl: Decimal

    @property
    def is_closed(self) -> bool:
        return self.exit_price is not None


def _sign(value: int) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def extract_trades_from_positions(
    positions: npt.NDArray[np.int64],
    closes: npt.NDArray[np.float64],
    multiplier: float,
) -> list[Trade]:
    """Round-trips from an evaluator's per-bar ``positions`` + the close series.

    Mirrors ``research.screen.daily_eval.evaluate_daily``'s convention exactly:
    ``positions[t]`` is the position held over ``(t-1, t]`` (the target decided at
    ``t-1``), so a position that becomes non-zero at index ``i`` was entered at
    ``close[i-1]`` and one that returns to zero at index ``j`` was exited at
    ``close[j-1]``. P&L of a round-trip is then ``qty * multiplier *
    (exit_close - entry_close)`` — identical to the equity curve's step sum.
    """
    n = int(positions.shape[0])
    trades: list[Trade] = []
    entry_idx: int | None = None
    entry_qty = 0
    entry_px = Decimal("0")

    def _open(i: int, qty: int) -> tuple[int, int, Decimal]:
        return i, qty, Decimal(str(float(closes[i - 1])))

    def _close(j: int) -> Trade:
        exit_px = Decimal(str(float(closes[j - 1])))
        pnl = Decimal(entry_qty) * Decimal(str(multiplier)) * (exit_px - entry_px)
        return Trade(
            entry_index=entry_idx if entry_idx is not None else j,
            entry_price=entry_px,
            exit_price=exit_px,
            quantity=abs(entry_qty),
            direction=_sign(entry_qty),
            pnl=pnl,
        )

    for i in range(1, n):
        prev = int(positions[i - 1])
        cur = int(positions[i])
        if prev == 0 and cur != 0:
            entry_idx, entry_qty, entry_px = _open(i, cur)
        elif prev != 0 and cur == 0:
            trades.append(_close(i))
            entry_idx, entry_qty, entry_px = None, 0, Decimal("0")
        elif prev != 0 and cur != 0 and _sign(prev) != _sign(cur):
            trades.append(_close(i))  # flip: close then re-open at the same bar
            entry_idx, entry_qty, entry_px = _open(i, cur)

    if entry_idx is not None:  # open position at the end — MTM to the last close
        last_close = Decimal(str(float(closes[n - 1])))
        pnl = Decimal(entry_qty) * Decimal(str(multiplier)) * (last_close - entry_px)
        trades.append(
            Trade(
                entry_index=entry_idx,
                entry_price=entry_px,
                exit_price=None,
                quantity=abs(entry_qty),
                direction=_sign(entry_qty),
                pnl=pnl,
            )
        )
    return trades


def lean_trade_to_trade(lt: LeanTrade) -> Trade:
    """Adapt a parsed :class:`LeanTrade` to the comparison :class:`Trade`."""
    return Trade(
        entry_index=-1,
        entry_price=lt.entry_price,
        exit_price=lt.exit_price,
        quantity=lt.quantity,
        direction=lt.direction,
        pnl=lt.profit_loss,
    )


def slippage_bps(price_a: Decimal, price_b: Decimal) -> Decimal:
    """Absolute basis-point difference of two prices (``price_b`` as reference)."""
    if price_b == 0:
        return Decimal("0") if price_a == 0 else Decimal("Infinity")
    return abs(price_a - price_b) / abs(price_b) * Decimal("10000")


@dataclass(frozen=True, slots=True)
class LegSlippage:
    """Per-leg slippage record (for an operator-legible parity report)."""

    pair_index: int
    leg: str  # "entry" | "exit"
    research_price: Decimal
    lean_price: Decimal
    bps: Decimal


@dataclass(frozen=True, slots=True)
class ParityVerdict:
    """The structured outcome of a parity comparison."""

    passed: bool
    research_trade_count: int
    lean_trade_count: int
    count_fraction: float
    count_ok: bool
    research_pnl: Decimal
    lean_pnl: Decimal
    pnl_fraction: Decimal
    pnl_ok: bool
    worst_slippage_bps: Decimal
    slippage_ok: bool
    leg_slippages: tuple[LegSlippage, ...]

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"PARITY {verdict}: "
            f"trades research={self.research_trade_count} lean={self.lean_trade_count} "
            f"(Δ{self.count_fraction:.1%} {'ok' if self.count_ok else 'OVER'}); "
            f"P&L Δ{self.pnl_fraction:.4%} {'ok' if self.pnl_ok else 'OVER'}; "
            f"worst slippage {self.worst_slippage_bps:.2f}bps "
            f"{'ok' if self.slippage_ok else 'OVER'}"
        )


def check_parity(
    research_result: BacktestResult,
    research_trades: list[Trade],
    lean_result: BacktestResult,
    lean_trades: list[Trade],
    *,
    starting_equity: Decimal,
    max_slippage_bps: Decimal = MAX_SLIPPAGE_BPS,
    max_pnl_fraction: Decimal = MAX_PNL_FRACTION,
    max_count_fraction: float = MAX_COUNT_FRACTION,
) -> ParityVerdict:
    """Apply the three locked §6.6 criteria; return a structured verdict.

    Count + slippage compare CLOSED trades (matched pairwise by entry order). The
    aggregate-P&L check uses each engine's equity-derived total P&L so an open
    final position is accounted for (closed-trade sums would miss it).
    """
    research_closed = [t for t in research_trades if t.is_closed]
    lean_closed = [t for t in lean_trades if t.is_closed]

    # 1. trade-count divergence.
    denom = max(len(lean_closed), 1)
    count_fraction = abs(len(research_closed) - len(lean_closed)) / denom
    count_ok = count_fraction <= max_count_fraction

    # 2. aggregate P&L divergence (equity-derived).
    research_pnl = Decimal(str(research_result.pnl))
    lean_pnl = Decimal(str(lean_result.pnl))
    pnl_fraction = (
        abs(research_pnl - lean_pnl) / starting_equity if starting_equity != 0 else Decimal("0")
    )
    pnl_ok = pnl_fraction <= max_pnl_fraction

    # 3. per-trade slippage (matched pairwise by entry order).
    leg_slippages: list[LegSlippage] = []
    worst = Decimal("0")
    for idx, (r, lake) in enumerate(zip(research_closed, lean_closed, strict=False)):
        entry = slippage_bps(r.entry_price, lake.entry_price)
        leg_slippages.append(LegSlippage(idx, "entry", r.entry_price, lake.entry_price, entry))
        worst = max(worst, entry)
        if r.exit_price is not None and lake.exit_price is not None:
            ex = slippage_bps(r.exit_price, lake.exit_price)
            leg_slippages.append(LegSlippage(idx, "exit", r.exit_price, lake.exit_price, ex))
            worst = max(worst, ex)
    slippage_ok = worst <= max_slippage_bps

    return ParityVerdict(
        passed=count_ok and pnl_ok and slippage_ok,
        research_trade_count=len(research_closed),
        lean_trade_count=len(lean_closed),
        count_fraction=count_fraction,
        count_ok=count_ok,
        research_pnl=research_pnl,
        lean_pnl=lean_pnl,
        pnl_fraction=pnl_fraction,
        pnl_ok=pnl_ok,
        worst_slippage_bps=worst,
        slippage_ok=slippage_ok,
        leg_slippages=tuple(leg_slippages),
    )
