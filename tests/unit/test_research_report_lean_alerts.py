"""The ``engine: lean`` report path — RED liquidation banner + authoritative flag.

Covers the ``write_report`` extension that surfaces LEAN-native margin-call /
forced-liquidation orders un-missably (design §6.2) and marks an all-LEAN report
authoritative (numpy-screen reports stay non-authoritative).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from research.eval.report import ReportRow, write_report

_GENERATED = datetime(2024, 1, 1, tzinfo=UTC)


def _row(fill: str = "lean") -> ReportRow:
    return ReportRow(
        symbol="/MES",
        strategy_name="donchian(20,1)",
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        bars=2,
        start_price=0.0,
        end_price=0.0,
        price_treatment="continuous RAW · LEAN fills",
        fill=fill,
        metrics={
            "total_return": 0.1,
            "cagr": 0.2,
            "max_drawdown_pct": 0.05,
            "pnl": 1000.0,
            "final_equity": 11000.0,
        },
        equity_curve=np.array([10000.0, 11000.0], dtype=np.float64),
    )


def test_liquidation_alert_renders_red_banner(tmp_path: Path) -> None:
    alert = "/MES: 1 LEAN margin-call/liquidation order(s); first 2024-01-15"
    write_report(
        tmp_path,
        run_name="lean_run",
        harness_version="test",
        generated_at=_GENERATED,
        rows=[_row()],
        alerts=(alert,),
    )
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))

    assert "LIQUIDATION DETECTED" in html
    assert "class='ruin'" in html  # the red-banner style fires
    assert "2024-01-15" in html
    assert "LIQUIDATION DETECTED" in md
    # The header names BOTH ruin signals — the alert can be an equity wipe with no
    # tagged margin-call order (see run.lean_ruin_alert).
    assert "equity wipe" in html and "equity wipe" in md
    assert result["liquidation_alerts"] == [alert]
    assert result["authoritative"] is True  # all rows are LEAN fills


def test_no_alerts_no_banner(tmp_path: Path) -> None:
    write_report(
        tmp_path,
        run_name="lean_run",
        harness_version="test",
        generated_at=_GENERATED,
        rows=[_row()],
    )
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert "LIQUIDATION DETECTED" not in html
    assert "class='ruin'" not in html
    assert result["liquidation_alerts"] == []


def test_numpy_screen_report_stays_non_authoritative(tmp_path: Path) -> None:
    # A close-fill (numpy screen) report must NOT be marked authoritative.
    write_report(
        tmp_path,
        run_name="numpy_run",
        harness_version="test",
        generated_at=_GENERATED,
        rows=[_row(fill="close")],
    )
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["authoritative"] is False
