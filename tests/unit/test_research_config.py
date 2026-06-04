"""RunConfig parsing + validation (design §4.4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from research.config.schema import load_run_config, parse_run_config


def _valid_raw() -> dict[str, object]:
    return {
        "name": "smoke",
        "engine": "daily",
        "mode": "backtest",
        "resolution": "daily",
        "universe": ["TLT", "/MES"],
        "date_range": {"start": "2024-01-01", "end": "2025-12-31"},
        "strategy": {"ref": "buy_and_hold", "contracts": 2},
        "expiries": {"/MES": "202606"},
        "data_root": "some/copy/path",
    }


def test_parse_valid() -> None:
    cfg = parse_run_config(_valid_raw())
    assert cfg.name == "smoke"
    assert cfg.universe == ("TLT", "/MES")
    assert cfg.start == date(2024, 1, 1)
    assert cfg.end == date(2025, 12, 31)
    assert cfg.contracts == 2
    assert cfg.expiries == {"/MES": "202606"}
    assert cfg.data_root == Path("some/copy/path")


def test_defaults_applied() -> None:
    cfg = parse_run_config(
        {"name": "m", "universe": ["TLT"], "date_range": {"start": "2024-01-01"}}
    )
    assert cfg.engine == "daily"
    assert cfg.mode == "backtest"
    assert cfg.resolution == "daily"
    assert cfg.contracts == 1
    assert cfg.end is None


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        ({"name": ""}, "non-empty 'name'"),
        ({"universe": []}, "non-empty list"),
        ({"engine": "wat"}, "engine="),
        ({"resolution": "second"}, "resolution="),
        ({"mode": "sideways"}, "mode="),
        ({"strategy": {"ref": "mystery"}}, "strategy.ref="),
        ({"strategy": {"ref": "buy_and_hold", "contracts": 0}}, "non-zero int"),
        ({"strategy": {"ref": "buy_and_hold", "contracts": True}}, "non-zero int"),
        ({"date_range": {"start": "2025-01-01", "end": "2024-01-01"}}, "precedes start"),
    ],
)
def test_validation_errors(mutate: dict[str, object], match: str) -> None:
    raw = _valid_raw()
    raw.update(mutate)
    with pytest.raises(ValueError, match=match):
        parse_run_config(raw)


def test_load_from_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "run.yaml"
    path.write_text(
        "name: from_file\n"
        "universe: ['TLT']\n"
        "date_range: {start: 2024-01-01}\n"
        "strategy: {ref: buy_and_hold}\n",
        encoding="utf-8",
    )
    cfg = load_run_config(path)
    assert cfg.name == "from_file"
    assert cfg.universe == ("TLT",)
    assert cfg.start == date(2024, 1, 1)
