"""Daily intrabar liquidation estimator + margin reference (design §6.3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np

from research.data.bars import BarSeries
from research.data.contract_specs import get_spec
from research.eval.results import BacktestResult
from research.risk.liquidation import (
    FUTURES_MAINTENANCE_MARGIN_USD,
    estimate_intrabar_liquidation,
    load_lean_maintenance_margin,
    margin_model_for,
)


def _series(close: list[float], low: list[float], high: list[float]) -> BarSeries:
    n = len(close)
    dates = tuple(
        date(2026, 1, 1) + (date(2026, 12, 31) - date(2026, 1, 1)) * i // (n - 1) for i in range(n)
    )
    return BarSeries(
        symbol="/MES",
        dates=dates,
        open=np.asarray(close, dtype=np.float64),
        high=np.asarray(high, dtype=np.float64),
        low=np.asarray(low, dtype=np.float64),
        close=np.asarray(close, dtype=np.float64),
        volume=np.ones(n),
    )


def _result(
    symbol: str, positions: list[int], equity: list[float], dates: tuple[date, ...]
) -> BacktestResult:
    return BacktestResult(
        symbol=symbol,
        dates=dates,
        equity_curve=np.asarray(equity, dtype=np.float64),
        positions=np.asarray(positions, dtype=np.int64),
        starting_cash=equity[0],
        multiplier=float(get_spec(symbol).multiplier),
        fill="close",
        strategy_name="t",
    )


def test_margin_model_futures_uses_fixed_reference() -> None:
    model = margin_model_for(get_spec("/MES"))
    assert model.is_future is True
    assert model.maintenance_per_contract == FUTURES_MAINTENANCE_MARGIN_USD["/MES"]
    # Fixed $/contract regardless of price.
    assert model.maintenance_per_contract_at(5000.0, 5.0) == float(
        FUTURES_MAINTENANCE_MARGIN_USD["/MES"]
    )


def test_margin_model_etf_is_fraction_of_notional() -> None:
    model = margin_model_for(get_spec("TLT"))
    assert model.is_future is False
    # 25% of (price * multiplier=1).
    assert model.maintenance_per_contract_at(90.0, 1.0) == 0.25 * 90.0


def test_high_leverage_breaches_maintenance_is_flagged() -> None:
    # 100 /MES contracts on $100k (≈25x): maintenance 100*1080=108k > equity ⇒ flagged.
    series = _series(close=[5000.0, 5000.0], low=[4990.0, 4995.0], high=[5010.0, 5010.0])
    result = _result("/MES", [0, 100], [100_000.0, 100_000.0], series.dates)
    est = estimate_intrabar_liquidation(result, series, margin_model_for(get_spec("/MES")))
    assert est.liquidated is True
    assert est.n_flagged == 1
    assert est.first_flag is not None
    assert est.first_flag.shortfall > 0.0
    assert "minute" in est.residual_uncertainty.lower()  # the P5 caveat is present


def test_small_position_not_flagged() -> None:
    series = _series(close=[5000.0, 5000.0], low=[4990.0, 4980.0], high=[5010.0, 5010.0])
    result = _result("/MES", [0, 1], [100_000.0, 100_000.0], series.dates)
    est = estimate_intrabar_liquidation(result, series, margin_model_for(get_spec("/MES")))
    assert est.liquidated is False
    assert est.n_flagged == 0


def test_load_lean_maintenance_margin_reads_last_row(tmp_path: Path) -> None:
    csv_dir = tmp_path / "future" / "cme" / "margins"
    csv_dir.mkdir(parents=True)
    (csv_dir / "MES.csv").write_text(
        "date,initial,maintenance\n20200101,700,640\n20220121,1188,1080\n", encoding="utf-8"
    )
    assert load_lean_maintenance_margin(tmp_path, get_spec("/MES")) == Decimal("1080")


def test_load_lean_maintenance_margin_absent_returns_none(tmp_path: Path) -> None:
    assert load_lean_maintenance_margin(tmp_path, get_spec("/MES")) is None
    assert load_lean_maintenance_margin(tmp_path, get_spec("TLT")) is None  # ETFs: no fixed margin
