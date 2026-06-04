"""Buy-and-hold exactness — the P1 data-plumbing proof (design §8).

The whole point of P1: prove the loader → evaluator chain reproduces a
hand-computable buy-and-hold result EXACTLY, on both an ETF and a future, and
that the generic numpy evaluator agrees with the direct close-to-close
computation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np

from research.data.contract_specs import get_spec
from research.data.daily_loader import load_daily_series
from research.screen.daily_eval import evaluate_daily
from research.strategy.buy_and_hold import BuyAndHold, buy_and_hold_result
from tests.unit._research_fixtures import write_equity_daily, write_futures_daily

# close: 100 → 110 → 105 → 120
_ETF_BARS = [
    (date(2024, 1, 2), 100.0, 100.0, 100.0, 100.0, 1_000_000.0),
    (date(2024, 1, 3), 100.0, 110.0, 100.0, 110.0, 1_000_000.0),
    (date(2024, 1, 4), 110.0, 110.0, 105.0, 105.0, 1_000_000.0),
    (date(2024, 1, 5), 105.0, 120.0, 105.0, 120.0, 1_000_000.0),
]
# /MES close: 5000 → 5010 → 4990 → 5025
_FUT_BARS = [
    (date(2024, 1, 2), 5000.0, 5000.0, 5000.0, 5000.0, 10000.0),
    (date(2024, 1, 3), 5000.0, 5010.0, 5000.0, 5010.0, 10000.0),
    (date(2024, 1, 4), 5010.0, 5010.0, 4990.0, 4990.0, 10000.0),
    (date(2024, 1, 5), 4990.0, 5025.0, 4990.0, 5025.0, 10000.0),
]


def test_etf_buy_and_hold_exact(tmp_path: Path) -> None:
    write_equity_daily(tmp_path, "TLT", _ETF_BARS)
    series = load_daily_series(tmp_path, "TLT")
    spec = get_spec("TLT")  # multiplier 1
    res = buy_and_hold_result(series, multiplier=spec.multiplier, contracts=1)
    # P&L = 1 * 1 * (120 - 100) = 20; entry-notional cash = 100 ⇒ +20%.
    assert res.pnl == 20.0
    assert res.total_return == (120.0 / 100.0 - 1.0)
    assert res.starting_cash == 100.0


def test_futures_buy_and_hold_exact(tmp_path: Path) -> None:
    write_futures_daily(tmp_path, "/MES", "202403", _FUT_BARS, market_dir="cme")
    series = load_daily_series(tmp_path, "/MES", expiry="202403")
    spec = get_spec("/MES")  # multiplier 5
    res = buy_and_hold_result(series, multiplier=spec.multiplier, contracts=2)
    # P&L = 2 contracts * $5 * (5025 - 5000) = $250 exactly.
    assert res.pnl == 250.0


def test_evaluator_agrees_with_direct_buy_and_hold(tmp_path: Path) -> None:
    write_equity_daily(tmp_path, "TLT", _ETF_BARS)
    series = load_daily_series(tmp_path, "TLT")
    mult = float(get_spec("TLT").multiplier)
    direct = buy_and_hold_result(series, multiplier=Decimal("1"), contracts=1)
    via_eval = evaluate_daily(
        series, BuyAndHold(1), multiplier=mult, starting_cash=direct.starting_cash
    )
    assert np.array_equal(direct.equity_curve, via_eval.equity_curve)
    assert via_eval.pnl == direct.pnl


def test_short_buy_and_hold_sign(tmp_path: Path) -> None:
    # Holding -1 contract on a rising market loses money.
    write_futures_daily(tmp_path, "/MES", "202403", _FUT_BARS, market_dir="cme")
    series = load_daily_series(tmp_path, "/MES", expiry="202403")
    res = buy_and_hold_result(series, multiplier=Decimal("5"), contracts=-1)
    assert res.pnl == -125.0  # -1 * 5 * (5025 - 5000)
