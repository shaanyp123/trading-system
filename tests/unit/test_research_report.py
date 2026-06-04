"""Report writer handles non-finite metrics without emitting `$inf`/invalid JSON."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from research.eval.report import ReportRow, write_report


def _row(metrics: dict[str, float]) -> ReportRow:
    return ReportRow(
        symbol="TLT",
        strategy_name="buy_and_hold(1)",
        start=date(2024, 1, 2),
        end=date(2024, 1, 4),
        bars=3,
        start_price=100.0,
        end_price=120.0,
        price_treatment="raw",
        fill="close",
        metrics=metrics,
        equity_curve=np.array([100.0, 110.0, 120.0], dtype=np.float64),
    )


def test_non_finite_metrics_render_safely(tmp_path: Path) -> None:
    row = _row(
        {
            "total_return": 0.2,
            "cagr": float("nan"),
            "max_drawdown_pct": 0.0,
            "pnl": float("inf"),
            "final_equity": float("inf"),
        }
    )
    html_path = write_report(
        tmp_path / "run",
        run_name="nonfinite",
        harness_version="test",
        generated_at=datetime.now(UTC),
        rows=[row],
    )
    # result.json must be valid JSON (non-finite → null, never NaN/Infinity tokens).
    result = json.loads((html_path.parent / "result.json").read_text(encoding="utf-8"))
    m = result["instruments"][0]["metrics"]
    assert m["cagr"] is None and m["pnl"] is None

    html = html_path.read_text(encoding="utf-8")
    assert "$inf" not in html and "$nan" not in html.lower()
    assert "n/a" in html  # cagr + pnl render as n/a
