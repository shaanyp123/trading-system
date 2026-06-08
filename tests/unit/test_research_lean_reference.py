"""Unit tests for the LEAN reference run-spec builder (research/lean/reference.py).

LEAN-free: pins the ``RunConfig`` → ``LeanRunSpec`` mapping and the docker-argv
isolation invariants for a reference-strategy run (the §9-P2 command-assembly safety
checks, mirrored from test_research_lean_driver.py). The REAL engine run is the
operator's acceptance gate (tests/integration/test_research_lean_engine.py skips
without a backend — never a silent pass).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research.config.schema import RunConfig, parse_run_config
from research.lean.driver import build_run_argv
from research.lean.reference import build_reference_run_spec


def _cfg(tmp_path: Path, **overrides: Any) -> RunConfig:
    raw: dict[str, Any] = {
        "name": "lean_test",
        "engine": "lean",
        "mode": "backtest",
        "resolution": "daily",
        "universe": ["/MES"],
        "date_range": {"start": "2023-09-01", "end": "2026-06-03"},
        "starting_cash": 100000,
        "strategy": {"ref": "donchian", "channel": 20, "contracts": 1},
        "costs": {"model": "ibkr"},
        "data_root": str(tmp_path),
    }
    raw.update(overrides)
    return parse_run_config(raw)


def test_reference_spec_donchian_future(tmp_path: Path) -> None:
    spec = build_reference_run_spec(_cfg(tmp_path), "/MES")
    assert spec.algorithm_type_name == "ResearchRunnerAlgorithm"
    assert spec.algorithm_source.name == "research_runner.py"
    assert spec.symbol == "/MES"
    assert spec.multiplier == 5.0  # /MES point value (contract_specs)
    assert spec.strategy_name == "donchian(20,1)"
    assert spec.posts_to_api is False
    assert spec.extra_package_mounts == ()  # self-contained — no strategies/ mount
    p = spec.parameters
    assert p["RESEARCH_SYMBOL"] == "/MES"
    assert p["STRATEGY_REF"] == "donchian"
    assert p["DONCHIAN_CHANNEL"] == "20"
    assert p["CONTRACTS"] == "1"
    assert p["START_DATE"] == "20230901"
    assert p["END_DATE"] == "20260603"
    assert p["STARTING_CASH_USD"] == "100000"
    assert p["RESOLUTION"] == "daily"
    assert p["COSTS_MODEL"] == "ibkr"


def test_reference_spec_buy_and_hold_etf(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, universe=["TLT"], strategy={"ref": "buy_and_hold", "contracts": 2})
    spec = build_reference_run_spec(cfg, "TLT")
    assert spec.symbol == "TLT"
    assert spec.multiplier == 1.0
    assert spec.strategy_name == "buy_and_hold(2)"
    assert spec.parameters["STRATEGY_REF"] == "buy_and_hold"
    assert spec.parameters["CONTRACTS"] == "2"


def test_reference_spec_long_only_uses_size_magnitude(tmp_path: Path) -> None:
    # Reference strategies are long-only; a negative `contracts` becomes magnitude.
    cfg = _cfg(tmp_path, strategy={"ref": "donchian", "channel": 10, "contracts": -3})
    spec = build_reference_run_spec(cfg, "/MES")
    assert spec.parameters["CONTRACTS"] == "3"
    assert spec.strategy_name == "donchian(10,3)"


def test_reference_spec_passes_costs_model(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, costs={"model": "zero"})
    spec = build_reference_run_spec(cfg, "/MES")
    assert spec.parameters["COSTS_MODEL"] == "zero"


def test_reference_spec_requires_end_date(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, date_range={"start": "2023-09-01"})  # no end
    with pytest.raises(ValueError, match=r"needs date_range\.end"):
        build_reference_run_spec(cfg, "/MES")


def test_reference_spec_rejects_v1_adapter(tmp_path: Path) -> None:
    # V1 runs the real lean/v1_strategy.py via reproduce_v1, not the generic runner.
    cfg = _cfg(tmp_path, strategy={"ref": "v1_adapter"})
    with pytest.raises(ValueError, match="reproduce_v1"):
        build_reference_run_spec(cfg, "/MES")


def test_reference_run_docker_argv_is_isolated(tmp_path: Path) -> None:
    spec = build_reference_run_spec(_cfg(tmp_path), "/MES")
    argv = build_run_argv(
        spec,
        tmp_path / "project",
        tmp_path / "out",
        backend="docker",
        image="trading-lean-local:latest",
    )
    joined = " ".join(argv)
    # Isolation: no prod network, POST stub, dummy bearer (design §9 P2).
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "LEAN_LOCAL_API_BASE_URL=http://127.0.0.1:9" in argv
    assert any(a.startswith("LEAN_LOCAL_BEARER_TOKEN=") for a in argv)
    # Anti-pollution: the prod api + the live volume never appear in the argv.
    assert "api:8000" not in joined
    assert "trading_lean_data" not in joined
    # The runner source is mounted at the entrypoint's hard-coded algorithm path.
    assert any("research_runner.py:/Lean/Algorithm/v1_strategy.py" in a for a in argv)
