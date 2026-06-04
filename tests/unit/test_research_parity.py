"""§6.6 parity comparison logic + numpy Donchian (design §6.6, P2).

LEAN-free coverage of the parity *logic*: the numpy Donchian reference runs through
``evaluate_daily``, trades are extracted, and ``check_parity`` is exercised on the
pass case + a failure of each locked criterion. The integration test
(``test_vbt_lean_parity.py``) wires real LEAN output through the same functions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import numpy.typing as npt

from research.data.bars import BarSeries
from research.eval.parity import (
    Trade,
    check_parity,
    extract_trades_from_positions,
    slippage_bps,
)
from research.eval.results import BacktestResult
from research.screen.daily_eval import evaluate_daily
from research.strategy.donchian import DonchianBreakout

# A series that breaks out (enter long) then breaks down (exit) under channel=3.
_HIGH = [101, 101, 101, 101, 104, 106, 107, 106, 102, 100]
_LOW = [99, 99, 99, 99, 100, 103, 104, 100, 98, 98]
_CLOSE = [100, 100, 100, 100, 103, 105, 106, 101, 99, 99]


def _series() -> BarSeries:
    n = len(_CLOSE)
    return BarSeries(
        symbol="TLT",
        dates=tuple(date(2024, 1, 1 + i) for i in range(n)),
        open=np.asarray(_CLOSE, dtype=np.float64),
        high=np.asarray(_HIGH, dtype=np.float64),
        low=np.asarray(_LOW, dtype=np.float64),
        close=np.asarray(_CLOSE, dtype=np.float64),
        volume=np.ones(n, dtype=np.float64),
    )


def _flat_equity(pnl: float, *, start: float = 100_000.0, n: int = 10) -> npt.NDArray[np.float64]:
    curve = np.full(n, start, dtype=np.float64)
    curve[-1] = start + pnl
    return curve


def _lean_result(pnl: float, *, start: float = 100_000.0) -> BacktestResult:
    return BacktestResult(
        symbol="TLT",
        dates=tuple(date(2024, 1, 1 + i) for i in range(10)),
        equity_curve=_flat_equity(pnl, start=start),
        positions=np.zeros(10, dtype=np.int64),
        starting_cash=start,
        multiplier=1.0,
        fill="lean",
        strategy_name="donchian(3,1)",
    )


def test_donchian_targets() -> None:
    targets = DonchianBreakout(channel=3, contracts=1).target_positions(_series())
    assert targets.tolist() == [0, 0, 0, 0, 1, 1, 1, 1, 0, 0]


def test_extract_trades_matches_evaluator_pnl() -> None:
    series = _series()
    result = evaluate_daily(series, DonchianBreakout(channel=3), multiplier=1.0)
    trades = extract_trades_from_positions(result.positions, series.close, 1.0)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.is_closed
    assert trade.entry_price == Decimal("103.0")
    assert trade.exit_price == Decimal("99.0")
    assert trade.quantity == 1
    assert trade.direction == 1
    assert trade.pnl == Decimal("-4.0")
    # Sum of trade P&L == the evaluator's total P&L (no open position remains).
    assert trade.pnl == Decimal(str(result.pnl))


def test_extract_trades_open_position_at_end() -> None:
    # positions never returns to 0 ⇒ one OPEN trade, MTM'd to the last close.
    positions = np.array([0, 0, 1, 1, 1], dtype=np.int64)
    closes = np.array([10.0, 11.0, 12.0, 13.0, 15.0], dtype=np.float64)
    trades = extract_trades_from_positions(positions, closes, 2.0)
    assert len(trades) == 1
    assert not trades[0].is_closed
    assert trades[0].entry_price == Decimal("11.0")  # close[index-1] of first nonzero
    assert trades[0].pnl == Decimal("8.0")  # 1 * 2.0 * (15 - 11)


def test_slippage_bps() -> None:
    assert slippage_bps(Decimal("100.05"), Decimal("100")) == Decimal("5")
    assert slippage_bps(Decimal("100"), Decimal("100")) == Decimal("0")
    assert slippage_bps(Decimal("1"), Decimal("0")).is_infinite()


def _research_side() -> tuple[BacktestResult, list[Trade]]:
    series = _series()
    result = evaluate_daily(series, DonchianBreakout(channel=3), multiplier=1.0)
    trades = extract_trades_from_positions(result.positions, series.close, 1.0)
    return result, trades


def test_check_parity_pass() -> None:
    research_result, research_trades = _research_side()
    # LEAN: same P&L, entry/exit within 5bps, same trade count.
    lean_trades = [
        Trade(
            entry_index=-1,
            entry_price=Decimal("103.01"),  # 0.97 bps vs 103.00
            exit_price=Decimal("99.00"),
            quantity=1,
            direction=1,
            pnl=Decimal("-4"),
        )
    ]
    verdict = check_parity(
        research_result,
        research_trades,
        _lean_result(-4.0),
        lean_trades,
        starting_equity=Decimal("100000"),
    )
    assert verdict.passed, verdict.summary()
    assert verdict.count_ok and verdict.pnl_ok and verdict.slippage_ok


def test_check_parity_fails_trade_count() -> None:
    research_result, research_trades = _research_side()
    verdict = check_parity(
        research_result, research_trades, _lean_result(-4.0), [], starting_equity=Decimal("100000")
    )
    assert not verdict.passed
    assert not verdict.count_ok


def test_check_parity_fails_pnl() -> None:
    research_result, research_trades = _research_side()
    lean_trades = [Trade(-1, Decimal("103.0"), Decimal("99.0"), 1, 1, Decimal("-604"))]
    # research pnl -4, lean equity pnl -604 ⇒ |600|/100000 = 0.6% > 0.5%.
    verdict = check_parity(
        research_result,
        research_trades,
        _lean_result(-604.0),
        lean_trades,
        starting_equity=Decimal("100000"),
    )
    assert not verdict.passed
    assert not verdict.pnl_ok


def test_check_parity_fails_slippage() -> None:
    research_result, research_trades = _research_side()
    lean_trades = [
        Trade(-1, Decimal("110.0"), Decimal("99.0"), 1, 1, Decimal("-4"))  # ~680 bps entry slip
    ]
    verdict = check_parity(
        research_result,
        research_trades,
        _lean_result(-4.0),
        lean_trades,
        starting_equity=Decimal("100000"),
    )
    assert not verdict.passed
    assert not verdict.slippage_ok
    assert verdict.worst_slippage_bps > Decimal("5")
