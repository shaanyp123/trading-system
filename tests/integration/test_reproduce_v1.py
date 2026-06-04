"""Reproduce production V1 in REAL LEAN + cross-check vs the live oracle (§8, P2).

Drives the production ``V1TrendFollowingAlgorithm`` (isolated: ``--network none`` +
POST stub + dummy bearer — design §9 P2) on the on-disk bar COPY, then checks its
entry DECISIONS agree with the live paper signals oracle. SKIPS (visibly) when the
docker backend, the data snapshot, or the captured oracle is absent — a missing
LEAN is never a silent pass. The parse + cross-check LOGIC is covered LEAN-free by
the golden test (tests/golden/test_research_v1_repro.py).

Operator inputs for the real run (P2 acceptance):
* build/tag the ``trading-lean-local`` image (infrastructure/lean_local/Dockerfile);
* snapshot the on-disk bars to ``research/data/cache/lean_bars`` (research/README);
* capture the oracle to ``$RESEARCH_V1_ORACLE`` via the SQL in research/lean/README;
* optionally set ``$RESEARCH_V1_WINDOW`` = ``YYYY-MM-DD:YYYY-MM-DD`` (single-phash).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from research.eval.reproduce_v1 import (
    build_v1_run_spec,
    crosscheck_entries,
    entries_from_fills,
    load_oracle,
)
from research.lean import images
from research.lean.driver import DEFAULT_LEAN_LOCAL_IMAGE, run_backtest

pytestmark = pytest.mark.integration

_DATA_ROOT = Path(os.environ.get("RESEARCH_DATA_ROOT", "research/data/cache/lean_bars"))
_ORACLE_PATH = Path(os.environ.get("RESEARCH_V1_ORACLE", ""))
_MIN_MATCH_RATE = float(os.environ.get("RESEARCH_V1_MIN_MATCH_RATE", "0.9"))


def _window() -> tuple[date, date] | None:
    raw = os.environ.get("RESEARCH_V1_WINDOW", "")
    if not raw or ":" not in raw:
        return None
    start, end = raw.split(":", 1)
    return date.fromisoformat(start), date.fromisoformat(end)


def test_reproduce_v1_matches_oracle(tmp_path: Path) -> None:
    if images.runnable_backend("docker", lean_local_image=DEFAULT_LEAN_LOCAL_IMAGE) is None:
        pytest.skip(
            "V1 reproduction needs the docker backend + the "
            f"`{DEFAULT_LEAN_LOCAL_IMAGE}` image (the POST-stub env is only wired there): "
            f"{images.availability_report()}. Build it per research/lean/README. "
            "(design §9 P2; never a silent pass)"
        )
    if not _DATA_ROOT.exists():
        pytest.skip(f"data snapshot absent at {_DATA_ROOT} — see research/lean/README")
    if not _ORACLE_PATH.is_file():
        pytest.skip(
            "oracle snapshot absent — set $RESEARCH_V1_ORACLE to a capture of the prod "
            "signals table (SQL in research/lean/README)"
        )
    window = _window()
    if window is None:
        pytest.skip(
            "set $RESEARCH_V1_WINDOW=YYYY-MM-DD:YYYY-MM-DD to a SINGLE-phash sub-window "
            "— a full-window match is unreliable (parameter_set_hash varies per signal: "
            "params calibrated mid-window + the ER gate landed 2026-06-02). See "
            "research/lean/README → reproduce-V1."
        )

    spec = build_v1_run_spec(_DATA_ROOT)
    parsed = run_backtest(spec, work_dir=tmp_path, backend="docker")
    entries = entries_from_fills(list(parsed.fills))
    oracle = load_oracle(_ORACLE_PATH)
    report = crosscheck_entries(entries, oracle, window=window)
    assert report.match_rate >= _MIN_MATCH_RATE, report.summary()
