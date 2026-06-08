"""The P4 validity report writer (research/eval/report.py::write_validity_report)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from research.eval.compare import ComparisonRow
from research.eval.report import write_validity_report
from research.eval.sweep import SweepReport, SweepRow

_GENERATED = datetime(2024, 1, 1, tzinfo=UTC)
_ROWS = (
    SweepRow(label="channel=20", is_score=1.5, oos_score=0.30, degradation=1.2, n_folds=3),
    SweepRow(label="channel=10", is_score=1.8, oos_score=0.10, degradation=1.7, n_folds=3),
)
_COMPARISON = (
    ComparisonRow("channel=20", "candidate", 0.12, 0.05, 0.40, 0.08, 112_000.0),
    ComparisonRow("buy_and_hold", "benchmark", 0.20, 0.10, 0.60, 0.05, 120_000.0),
)


def _write(tmp_path: Path, *, null: float, comparison: tuple[ComparisonRow, ...]) -> Path:
    sweep = SweepReport(
        metric_name="sharpe",
        rows=_ROWS,
        n_trials=4,
        n_oos_obs=120,
        expected_max_under_null=null,
    )
    return write_validity_report(
        tmp_path,
        run_name="p4_test",
        harness_version="x",
        generated_at=_GENERATED,
        symbol="TLT",
        sweep=sweep,
        comparison=comparison,
    )


def test_ranked_table_mt_note_and_overfit_banner(tmp_path: Path) -> None:
    # best OOS 0.30 < null 0.50 ⇒ likely overfit ⇒ red banner.
    _write(tmp_path, null=0.50, comparison=_COMPARISON)
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))

    # Ranked table (top candidate first), multiple-testing note, overfit banner.
    assert "channel=20" in html and "channel=10" in html
    assert "multiple-testing" in html.lower()
    assert "LIKELY OVERFIT" in html and "class='ruin'" in html
    assert "LIKELY OVERFIT" in md
    # Benchmarks rendered.
    assert "buy_and_hold" in html
    # result.json shape.
    assert result["report_type"] == "validity_walk_forward"
    assert result["multiple_testing"]["likely_overfit"] is True
    assert result["multiple_testing"]["n_trials"] == 4
    assert result["ranking"][0]["params"] == "channel=20"  # ranked first
    assert result["ranking"][0]["oos_score"] == 0.30
    assert len(result["benchmarks"]) == 2


def test_no_overfit_banner_when_edge_beats_null(tmp_path: Path) -> None:
    # best OOS 0.30 > null 0.05 ⇒ not flagged.
    _write(tmp_path, null=0.05, comparison=())
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert "LIKELY OVERFIT" not in html
    assert "class='ruin'" not in html
    assert result["multiple_testing"]["likely_overfit"] is False
    assert result["benchmarks"] == []
