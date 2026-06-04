"""P3 leverage/ruin report: the RED liquidation banner is un-missable (design §6.2)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from research.eval.report import LeverageRow, write_leverage_report
from research.risk.metrics import RiskMetrics


def _row(cap: float, *, liquidated: bool, flags: int, lean_events: int = 0) -> LeverageRow:
    metrics = RiskMetrics(
        total_return=-0.3,
        cagr=-0.3,
        annualized_vol=0.5 * cap,
        sharpe=-0.1,
        sortino=-0.1,
        downside_deviation=0.4,
        max_drawdown_pct=0.4,
        max_drawdown_dollars=40_000.0 * cap,
        calmar=-0.5,
        volatility_drag=0.02 * cap * cap,
        longest_drawdown_days=120,
        worst_day=-0.05 * cap,
        worst_week=-0.1 * cap,
        var_5pct=-0.03,
        cvar_5pct=-0.05,
        p_drawdown_gt_20pct=0.9,
        p_drawdown_gt_50pct=0.5,
        risk_of_ruin=1.0 if liquidated else 0.5,
        peak_leverage=cap,
        mean_leverage=cap * 0.9,
        kelly_leverage=-1.0,
        fractional_kelly=-cap,
        margin_call_frequency=flags / 250.0,
        n_margin_events=flags,
        liquidated=liquidated,
        first_liquidation_date=date(2025, 6, 6) if liquidated else None,
    )
    return LeverageRow(
        leverage_cap=cap,
        scheme="fixed_fractional",
        metrics=metrics.to_report_dict(),
        liquidated=liquidated,
        first_liquidation_date=metrics.first_liquidation_date,
        n_liquidation_flags=flags,
        bars_evaluated=250,
        lean_margin_events=lean_events,
        residual_uncertainty="ESTIMATE, not a verdict ... re-run at MINUTE resolution.",
        leverage_curve=np.linspace(0.0, cap, 50),
    )


def test_report_renders_red_banner_when_liquidated(tmp_path: Path) -> None:
    rows = [
        _row(1.0, liquidated=False, flags=0),
        _row(3.0, liquidated=False, flags=0),
        _row(8.0, liquidated=True, flags=249),
    ]
    html_path = write_leverage_report(
        tmp_path,
        run_name="ruin_demo",
        harness_version="t",
        generated_at=datetime(2026, 6, 4, tzinfo=UTC),
        symbol="TLT",
        rows=rows,
    )
    html = html_path.read_text(encoding="utf-8")
    assert "LIQUIDATION DETECTED" in html
    assert "class='ruin'" in html  # the red banner div
    assert "8x" in html and "2025-06-06" in html
    # result.json records the machine verdict.
    payload = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["liquidation_detected"] is True
    assert payload["report_type"] == "leverage_sweep"
    assert {lvl["leverage_cap"] for lvl in payload["levels"]} == {1.0, 3.0, 8.0}


def test_report_no_banner_when_all_survive(tmp_path: Path) -> None:
    rows = [_row(1.0, liquidated=False, flags=0), _row(2.0, liquidated=False, flags=0)]
    write_leverage_report(
        tmp_path,
        run_name="safe",
        harness_version="t",
        generated_at=datetime(2026, 6, 4, tzinfo=UTC),
        symbol="TLT",
        rows=rows,
    )
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "LIQUIDATION DETECTED" not in html
    assert "class='ruin'" not in html
    payload = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["liquidation_detected"] is False


def test_lean_native_margin_event_also_trips_banner(tmp_path: Path) -> None:
    # A row with no estimator flags but a LEAN-native margin call still flags ruin.
    rows = [_row(4.0, liquidated=False, flags=0, lean_events=2)]
    write_leverage_report(
        tmp_path,
        run_name="lean_margin",
        harness_version="t",
        generated_at=datetime(2026, 6, 4, tzinfo=UTC),
        symbol="TLT",
        rows=rows,
    )
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "LIQUIDATION DETECTED" in html
    assert "LEAN margin-call" in html
