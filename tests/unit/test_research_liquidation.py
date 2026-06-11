"""Daily intrabar liquidation estimator + margin reference (design §6.3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from structlog.testing import capture_logs

from research.data.bars import BarSeries
from research.data.contract_specs import get_spec
from research.eval.results import BacktestResult
from research.risk.liquidation import (
    FUTURES_MAINTENANCE_MARGIN_USD,
    estimate_intrabar_liquidation,
    load_lean_maintenance_margin,
    margin_model_for,
)


def _series(
    close: list[float], low: list[float], high: list[float], symbol: str = "/MES"
) -> BarSeries:
    n = len(close)
    dates = tuple(
        date(2026, 1, 1) + (date(2026, 12, 31) - date(2026, 1, 1)) * i // (n - 1) for i in range(n)
    )
    return BarSeries(
        symbol=symbol,
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


def test_margin_model_etf_short_uses_reg_t_30pct() -> None:
    model = margin_model_for(get_spec("TLT"))
    # Reg-T: 25% of value long, 30% short; futures stay side-symmetric.
    assert model.maintenance_per_contract_at(100.0, 1.0, position=1) == pytest.approx(25.0)
    assert model.maintenance_per_contract_at(100.0, 1.0, position=-1) == pytest.approx(30.0)
    fut = margin_model_for(get_spec("/MES"))
    assert fut.maintenance_per_contract_at(5000.0, 5.0, position=-1) == float(
        FUTURES_MAINTENANCE_MARGIN_USD["/MES"]
    )


def test_etf_short_flags_at_30pct_where_long_would_not() -> None:
    # Mirrored ±1000-share TLT positions, same prior equity (28k): at the adverse
    # extreme MTM equity is 27k either side; the short bar is 30%·101·1000 = 30.3k
    # (flags) while the long bar is 25%·99·1000 = 24.75k (does not).
    series = _series(close=[100.0, 100.0], low=[99.0, 99.0], high=[101.0, 101.0], symbol="TLT")
    model = margin_model_for(get_spec("TLT"))
    short = _result("TLT", [0, -1000], [28_000.0, 28_000.0], series.dates)
    long_ = _result("TLT", [0, 1000], [28_000.0, 28_000.0], series.dates)
    assert estimate_intrabar_liquidation(short, series, model).liquidated is True
    assert estimate_intrabar_liquidation(long_, series, model).liquidated is False


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


def _write_margin_csv(data_root: Path) -> None:
    csv_dir = data_root / "future" / "cme" / "margins"
    csv_dir.mkdir(parents=True)
    (csv_dir / "MES.csv").write_text(
        "date,initial,maintenance\n20200101,700,640\n20220121,1188,1080\n", encoding="utf-8"
    )


def test_margin_schedule_stale_warning_fires_for_csv_vintage(tmp_path: Path) -> None:
    # CSV last-row vintage 2022-01-21 predates a 2024 window ⇒ warn; the margin
    # NUMBER itself is unchanged (warn-only, per design).
    _write_margin_csv(tmp_path)
    with capture_logs() as logs:
        model = margin_model_for(
            get_spec("/MES"), data_root=tmp_path, window_start=date(2024, 1, 1)
        )
    assert model.maintenance_per_contract == Decimal("1080")
    stale = [e for e in logs if e["event"] == "research_margin_schedule_stale"]
    assert len(stale) == 1
    assert stale[0]["symbol"] == "/MES"
    assert stale[0]["vintage"] == "2022-01-21"
    assert stale[0]["window_start"] == "2024-01-01"


def test_margin_schedule_stale_warning_fires_for_static_reference() -> None:
    # No CSV ⇒ the static dict resolves; its documented 2022-01 vintage predates 2024.
    with capture_logs() as logs:
        margin_model_for(get_spec("/MES"), window_start=date(2024, 1, 1))
    assert any(e["event"] == "research_margin_schedule_stale" for e in logs)


def test_margin_schedule_no_warning_when_window_inside_vintage(tmp_path: Path) -> None:
    _write_margin_csv(tmp_path)
    with capture_logs() as logs:
        margin_model_for(get_spec("/MES"), data_root=tmp_path, window_start=date(2021, 6, 1))
    assert not [e for e in logs if e["event"] == "research_margin_schedule_stale"]
