"""Reproduce-V1 logic: entry extraction, oracle load, cross-check (design §8)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from research.eval.reproduce_v1 import (
    Entry,
    OracleEntry,
    build_v1_run_spec,
    crosscheck_entries,
    entries_from_fills,
    load_oracle,
)
from research.lean.results import OrderFill


def _fill(market: str, day: date, qty: int) -> OrderFill:
    return OrderFill(market=market, fill_date=day, quantity=qty, direction=1 if qty > 0 else -1)


def test_entries_from_fills_flat_to_long() -> None:
    fills = [_fill("/MES", date(2026, 5, 28), 1), _fill("/M2K", date(2026, 5, 27), 1)]
    entries = entries_from_fills(fills)
    # Sorted by (date, market): M2K 05-27 first, then MES 05-28.
    assert entries == [
        Entry(date(2026, 5, 27), "/M2K", "long"),
        Entry(date(2026, 5, 28), "/MES", "long"),
    ]


def test_entries_from_fills_close_is_not_an_entry() -> None:
    fills = [_fill("/MES", date(2026, 5, 1), 1), _fill("/MES", date(2026, 5, 10), -1)]
    # Open then close to flat ⇒ exactly one entry (the open), the close is not one.
    assert entries_from_fills(fills) == [Entry(date(2026, 5, 1), "/MES", "long")]


def test_entries_from_fills_flip_counts_as_new_entry() -> None:
    fills = [_fill("/MES", date(2026, 5, 1), 1), _fill("/MES", date(2026, 5, 10), -2)]
    # +1 then -2 crosses flat to net short ⇒ a new short entry.
    assert entries_from_fills(fills) == [
        Entry(date(2026, 5, 1), "/MES", "long"),
        Entry(date(2026, 5, 10), "/MES", "short"),
    ]


def test_load_oracle_filters_non_entries(tmp_path: Path) -> None:
    payload = {
        "entries": [
            {"session_date": "2026-05-27", "market": "/M2K", "direction": "long"},
            {
                "session_date": "2026-05-30",
                "market": "/MES",
                "direction": "short",
                "signal_type": "exit",
            },
        ]
    }
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    oracle = load_oracle(path)
    assert oracle == [OracleEntry(date(2026, 5, 27), "/M2K", "long")]


def test_crosscheck_full_match() -> None:
    bt = [Entry(date(2026, 5, 27), "/M2K", "long"), Entry(date(2026, 5, 28), "/MES", "long")]
    oracle = [
        OracleEntry(date(2026, 5, 27), "/M2K", "long"),
        OracleEntry(date(2026, 5, 28), "/MES", "long"),
    ]
    report = crosscheck_entries(bt, oracle)
    assert report.match_rate == 1.0
    assert not report.missing_in_backtest and not report.extra_in_backtest


def test_crosscheck_reports_missing_and_extra() -> None:
    bt = [Entry(date(2026, 5, 28), "/MES", "long"), Entry(date(2026, 5, 29), "/MNQ", "long")]
    oracle = [
        OracleEntry(date(2026, 5, 27), "/M2K", "long"),
        OracleEntry(date(2026, 5, 28), "/MES", "long"),
    ]
    report = crosscheck_entries(bt, oracle)
    assert (date(2026, 5, 27), "/M2K", "long") in report.missing_in_backtest
    assert (date(2026, 5, 29), "/MNQ", "long") in report.extra_in_backtest
    assert report.match_rate == 0.5  # 1 of 2 oracle entries matched


def test_crosscheck_window_filter() -> None:
    bt = [Entry(date(2026, 5, 27), "/M2K", "long"), Entry(date(2026, 6, 2), "/MES", "long")]
    oracle = [
        OracleEntry(date(2026, 5, 27), "/M2K", "long"),
        OracleEntry(date(2026, 6, 2), "/MES", "long"),
    ]
    # Restrict to a single-phash sub-window (kickoff guidance) ⇒ only 05-27 compared.
    report = crosscheck_entries(bt, oracle, window=(date(2026, 5, 26), date(2026, 5, 28)))
    assert report.oracle_count == 1
    assert report.match_rate == 1.0


def test_build_v1_run_spec_is_isolated_and_uses_prod_inputs() -> None:
    spec = build_v1_run_spec(Path("research/data/cache/lean_bars"))
    assert spec.algorithm_type_name == "V1TrendFollowingAlgorithm"
    assert spec.algorithm_source == Path("lean/v1_strategy.py")  # read-only input, not copied
    assert spec.posts_to_api is True  # forces docker backend + isolation env
    assert Path("strategies") in spec.extra_package_mounts
    # Live params pulled from the production lean.json.
    assert spec.parameters["LOOKBACK_DAYS_DONCHIAN"] == "60"
    assert spec.parameters["EFFICIENCY_RATIO_THRESHOLD"] == "0.20"
