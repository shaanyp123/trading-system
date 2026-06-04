"""End-to-end P3 leverage sweep: config → sized sim → ruin report (the §9 P3 gate).

Uses the synthetic LEAN data fixtures (no Docker/LEAN, no gitignored snapshot) so the
``run_leverage_sweep`` glue is covered in CI: a config carrying ``sizing`` +
``leverage`` routes to the sweep, and the ETF 25%-maintenance cliff makes the high
cap trip the RED liquidation banner while the low cap survives.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from research.config.schema import parse_run_config
from research.run import run
from tests.unit._research_fixtures import write_equity_daily

# Moderate intraday range: 1x survives the ~4% lows, but 8x (maintenance ≥ equity on
# a 25%-maintenance ETF) is liquidated on every bar.
_BARS = [
    (date(2024, 1, 2), 100.0, 101.0, 99.0, 100.0, 1_000_000.0),
    (date(2024, 1, 3), 100.0, 101.0, 96.0, 98.0, 1_000_000.0),
    (date(2024, 1, 4), 98.0, 99.0, 95.0, 97.0, 1_000_000.0),
    (date(2024, 1, 5), 97.0, 98.0, 94.0, 96.0, 1_000_000.0),
]


def _sweep_cfg(root: Path) -> object:
    return parse_run_config(
        {
            "name": "ruin_sweep_e2e",
            "engine": "daily",
            "mode": "backtest",
            "resolution": "daily",
            "universe": ["TLT"],
            "date_range": {"start": "2024-01-01"},
            "strategy": {"ref": "buy_and_hold", "contracts": 1},
            "starting_cash": 100_000,
            "sizing": {"scheme": "fixed_fractional", "fraction": 10.0},
            "leverage": {"sweep": [1.0, 8.0]},
            "data_root": str(root),
        }
    )


def test_leverage_sweep_writes_ruin_report(tmp_path: Path) -> None:
    root = tmp_path / "lean_bars"
    write_equity_daily(root, "TLT", _BARS)

    html_path = run(_sweep_cfg(root), runs_dir=tmp_path / "runs")  # type: ignore[arg-type]
    assert html_path.name == "report.html"
    run_dir = html_path.parent

    payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["report_type"] == "leverage_sweep"
    assert payload["authoritative"] is False
    levels = {lvl["leverage_cap"]: lvl for lvl in payload["levels"]}
    assert set(levels) == {1.0, 8.0}
    # The cliff: 1x survives, 8x is liquidated (ETF maintenance ≥ equity above ~4x).
    assert levels[1.0]["liquidated"] is False
    assert levels[8.0]["liquidated"] is True
    assert payload["liquidation_detected"] is True

    html = html_path.read_text(encoding="utf-8")
    assert "LIQUIDATION DETECTED" in html and "8x" in html
