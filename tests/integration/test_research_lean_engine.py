"""Authoritative ``engine: lean`` path end-to-end — REAL run, skip-gated (design §9 P2).

Drives a reference strategy (Donchian) over a CONTINUOUS micro future through REAL
LEAN via :func:`research.run.run_lean`, on the read-only bar COPY, isolated
(``--network none`` + POST stub — the lean_local backend). SKIPS visibly when the
docker backend, the ``trading-lean-local`` image, or the data snapshot is absent —
a missing LEAN is never a silent pass. The wiring / spec assembly / report
surfacing are covered LEAN-free by the unit tests (test_research_lean_reference.py,
test_research_report_lean_alerts.py).

Operator inputs for the real run:
* build/tag the ``trading-lean-local`` image (infrastructure/lean_local/Dockerfile);
* snapshot the on-disk bars to ``research/data/cache/lean_bars`` (research/lean/README).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from research.config.schema import parse_run_config
from research.lean import images
from research.lean.driver import DEFAULT_LEAN_LOCAL_IMAGE
from research.run import run_lean

pytestmark = pytest.mark.integration

_DATA_ROOT = Path(os.environ.get("RESEARCH_DATA_ROOT", "research/data/cache/lean_bars"))


def test_engine_lean_runs_reference_over_continuous_future(tmp_path: Path) -> None:
    if images.runnable_backend("docker", lean_local_image=DEFAULT_LEAN_LOCAL_IMAGE) is None:
        pytest.skip(
            "engine=lean needs the docker backend + the "
            f"`{DEFAULT_LEAN_LOCAL_IMAGE}` image (the data COPY + isolation env are "
            f"only wired there): {images.availability_report()}. Build it per "
            "research/lean/README. (design §9 P2; never a silent pass)"
        )
    if not _DATA_ROOT.exists():
        pytest.skip(f"data snapshot absent at {_DATA_ROOT} — see research/lean/README")

    cfg = parse_run_config(
        {
            "name": "lean_engine_smoke",
            "engine": "lean",
            "mode": "backtest",
            "resolution": "daily",
            "universe": ["/MES"],
            "date_range": {"start": "2023-09-01", "end": "2026-06-03"},
            "starting_cash": 100000,
            "strategy": {"ref": "donchian", "channel": 20, "contracts": 1},
            "costs": {"model": "ibkr"},
            "data_root": str(_DATA_ROOT),
        }
    )
    report = run_lean(cfg, runs_dir=tmp_path)

    assert report.exists() and report.name == "report.html"
    result = json.loads((report.parent / "result.json").read_text(encoding="utf-8"))
    assert result["authoritative"] is True  # all rows are LEAN fills
    assert result["instruments"], "no instrument evaluated"
    inst = result["instruments"][0]
    assert inst["symbol"] == "/MES"
    assert inst["fill"] == "lean"
    # Continuous /MES stitches multi-year daily history (the point of the LEAN path
    # vs the numpy screen's single expiry) — expect well over a year of bars.
    assert inst["bars"] > 200, f"expected a multi-year continuous series, got {inst['bars']} bars"
