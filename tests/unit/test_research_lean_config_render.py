"""Throwaway lean.json rendering (design §4.3 config_render.py, P2)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from research.lean.config_render import (
    LeanProjectConfig,
    load_production_v1_parameters,
    reference_parameters,
    render_config,
    write_config,
)


def test_render_config_shape() -> None:
    cfg = LeanProjectConfig(
        algorithm_type_name="DonchianReferenceAlgorithm",
        algorithm_location="donchian_reference.py",
        parameters={"DONCHIAN_CHANNEL": "20", "CONTRACTS": "1"},
    )
    rendered = render_config(cfg)
    assert rendered["algorithm-language"] == "Python"
    assert rendered["algorithm-type-name"] == "DonchianReferenceAlgorithm"
    assert rendered["algorithm-location"] == "donchian_reference.py"
    assert rendered["environments"] == {"backtesting": {"live-mode": False}}
    assert rendered["parameters"] == {"DONCHIAN_CHANNEL": "20", "CONTRACTS": "1"}
    assert "results-destination-folder" not in rendered


def test_render_config_results_destination() -> None:
    cfg = LeanProjectConfig(
        algorithm_type_name="X",
        algorithm_location="x.py",
        parameters={"A": "1"},
        results_destination="/Results",
    )
    assert render_config(cfg)["results-destination-folder"] == "/Results"


def test_render_config_stringifies_values() -> None:
    cfg = LeanProjectConfig(
        algorithm_type_name="X",
        algorithm_location="x.py",
        parameters={"FLAG": True, "OFF": False, "RATIO": Decimal("0.20"), "N": 60},  # type: ignore[dict-item]
    )
    params = render_config(cfg)["parameters"]
    assert params == {"FLAG": "True", "OFF": "False", "RATIO": "0.20", "N": "60"}


def test_render_config_rejects_empty_params() -> None:
    cfg = LeanProjectConfig(algorithm_type_name="X", algorithm_location="x.py", parameters={})
    with pytest.raises(ValueError, match="non-empty"):
        render_config(cfg)


def test_write_config_writes_file(tmp_path: Path) -> None:
    cfg = LeanProjectConfig(
        algorithm_type_name="X", algorithm_location="x.py", parameters={"A": "1"}
    )
    path = write_config(tmp_path / "project", cfg)
    assert path.name == "lean.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["algorithm-type-name"] == "X"


def test_write_config_refuses_production_lean_dir() -> None:
    cfg = LeanProjectConfig(
        algorithm_type_name="X", algorithm_location="x.py", parameters={"A": "1"}
    )
    with pytest.raises(ValueError, match="production lean/ dir"):
        write_config(Path("lean"), cfg)


def test_load_production_v1_parameters_raises_post_decommission() -> None:
    # Crypto-pivot C0 (2026-07-08): production lean/lean.json was deleted
    # with the LEAN stack. The reproduce-V1 harness is retired-by-
    # construction; loading its production parameters now fails loudly
    # (never a silent fallback). The harness itself is removed alongside
    # strategies/v1_trend_following in the risk-review'd deletion PR.
    with pytest.raises(FileNotFoundError):
        load_production_v1_parameters()


def test_reference_parameters_injects_window_and_cash() -> None:
    params = reference_parameters(
        {"DONCHIAN_CHANNEL": 20, "RESEARCH_SYMBOL": "TLT"},
        start=date(2024, 1, 1),
        end=date(2025, 12, 31),
        starting_cash=100_000,
    )
    assert params["START_DATE"] == "20240101"
    assert params["END_DATE"] == "20251231"
    assert params["STARTING_CASH_USD"] == "100000"
    assert params["DONCHIAN_CHANNEL"] == "20"
    assert params["RESEARCH_SYMBOL"] == "TLT"
