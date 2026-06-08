"""Validity config parsing + run_validity routing (schema.py + run.py wiring).

Locks the P4 config surface (validity / strategy.sweep / benchmarks) and the
run-time guards the review flagged as untested.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from research.config.schema import parse_run_config
from research.run import _guard_phase, run
from tests.unit._research_fixtures import write_equity_daily


def _raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "validity",
        "engine": "daily",
        "mode": "backtest",
        "resolution": "daily",
        "universe": ["TLT"],
        "date_range": {"start": "2020-01-01", "end": "2024-01-01"},
        "strategy": {"ref": "donchian", "contracts": 1, "sweep": {"channel": [10, 20, 30]}},
        "validity": {"scheme": "walk_forward", "is_months": 24, "oos_months": 6, "step_months": 6},
        "benchmarks": ["buy_and_hold"],
    }
    raw.update(overrides)
    return raw


def test_parse_validity_sweep_and_benchmarks() -> None:
    cfg = parse_run_config(_raw())
    assert cfg.validity_scheme == "walk_forward"
    assert (cfg.is_months, cfg.oos_months, cfg.step_months) == (24, 6, 6)
    assert cfg.strategy_sweep == {"channel": (10.0, 20.0, 30.0)}
    assert cfg.benchmarks == ("buy_and_hold",)


def test_parse_rejects_unknown_validity_scheme() -> None:
    with pytest.raises(ValueError, match="walk_forward"):
        parse_run_config(_raw(validity={"scheme": "k_fold"}))


def test_parse_rejects_bool_in_sweep() -> None:
    with pytest.raises(ValueError, match="must be numbers"):
        parse_run_config(
            _raw(strategy={"ref": "donchian", "contracts": 1, "sweep": {"channel": [True, 20]}})
        )


def test_validity_with_lean_engine_is_rejected() -> None:
    # The sweep runs on the numpy screen (D2); pairing with engine: lean must error,
    # not silently run a plain LEAN backtest.
    cfg = parse_run_config(_raw(engine="lean"))
    with pytest.raises(NotImplementedError, match="engine: daily"):
        _guard_phase(cfg)


def test_run_validity_no_windows_errors(tmp_path: Path) -> None:
    root = tmp_path / "lean_bars"
    bars = [
        (date(2025, 1, 1) + timedelta(days=i), 100.0, 101.0, 99.0, 100.0, 1_000_000.0)
        for i in range(30)  # ~1 month — far too short for is=24 / oos=6 months
    ]
    write_equity_daily(root, "TLT", bars)
    cfg = parse_run_config(
        _raw(date_range={"start": "2025-01-01", "end": "2025-02-15"}, data_root=str(root))
    )
    with pytest.raises(ValueError, match="no walk-forward windows"):
        run(cfg, runs_dir=tmp_path / "runs")
