"""End-to-end P1 run: config → load → evaluate → report (the `make research` path)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from research.config.schema import RunConfig, parse_run_config
from research.run import run
from tests.unit._research_fixtures import write_equity_daily, write_futures_daily

_ETF_BARS = [
    (date(2024, 1, 2), 100.0, 100.0, 100.0, 100.0, 1_000_000.0),
    (date(2024, 1, 3), 100.0, 110.0, 100.0, 110.0, 1_000_000.0),
    (date(2024, 1, 4), 110.0, 120.0, 105.0, 120.0, 1_000_000.0),
]
_FUT_BARS = [
    (date(2024, 1, 2), 5000.0, 5000.0, 5000.0, 5000.0, 10000.0),
    (date(2024, 1, 3), 5000.0, 5030.0, 5000.0, 5030.0, 10000.0),
    (date(2024, 1, 4), 5030.0, 5050.0, 5010.0, 5025.0, 10000.0),
]


def _seed(tmp_path: Path) -> Path:
    root = tmp_path / "lean_bars"
    write_equity_daily(root, "TLT", _ETF_BARS)
    write_futures_daily(root, "/MES", "202606", _FUT_BARS, market_dir="cme")
    return root


def _cfg(root: Path, **overrides: object) -> RunConfig:
    raw: dict[str, object] = {
        "name": "e2e",
        "engine": "daily",
        "mode": "backtest",
        "resolution": "daily",
        "universe": ["TLT", "/MES"],
        "date_range": {"start": "2024-01-01"},
        "strategy": {"ref": "buy_and_hold", "contracts": 1},
        "expiries": {"/MES": "202606"},
        "data_root": str(root),
    }
    raw.update(overrides)
    return parse_run_config(raw)


def test_end_to_end_writes_report(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    html_path = run(_cfg(root), runs_dir=tmp_path / "runs")

    assert html_path.exists() and html_path.name == "report.html"
    run_dir = html_path.parent
    assert (run_dir / "report.md").exists()

    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["resolution"] == "daily"
    assert result["authoritative"] is False
    symbols = {inst["symbol"] for inst in result["instruments"]}
    assert symbols == {"TLT", "/MES"}

    html = html_path.read_text(encoding="utf-8")
    assert "TLT" in html and "<polyline" in html


def test_end_to_end_metrics_are_correct(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    html_path = run(_cfg(root), runs_dir=tmp_path / "runs")
    result = json.loads((html_path.parent / "result.json").read_text(encoding="utf-8"))
    by_symbol = {inst["symbol"]: inst for inst in result["instruments"]}
    # TLT 1 contract, entry-notional cash 100 ⇒ +20% (100 → 120).
    assert by_symbol["TLT"]["metrics"]["total_return"] == pytest.approx(0.2)
    # /MES 1 contract * $5 * (5025 - 5000) = $125 P&L.
    assert by_symbol["/MES"]["metrics"]["pnl"] == pytest.approx(125.0)


def test_lean_engine_points_to_p2(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    with pytest.raises(NotImplementedError, match="LEAN driver lands in P2"):
        run(_cfg(root, engine="lean"), runs_dir=tmp_path / "runs")


def test_intraday_resolution_points_to_p5(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    with pytest.raises(NotImplementedError, match="intraday is deferred"):
        run(_cfg(root, resolution="minute"), runs_dir=tmp_path / "runs")


def test_forward_mode_points_to_p7(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    with pytest.raises(NotImplementedError, match="P7"):
        run(_cfg(root, mode="forward"), runs_dir=tmp_path / "runs")


def test_missing_data_root_is_clear(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "does-not-exist")
    with pytest.raises(FileNotFoundError, match="data_root"):
        run(cfg, runs_dir=tmp_path / "runs")
