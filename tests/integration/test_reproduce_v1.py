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
* optionally set ``$RESEARCH_V1_WINDOW`` = ``YYYY-MM-DD:YYYY-MM-DD`` (a single
  parameter-REGIME window cut by date — prod phashes are per-signal, so the ER-gate
  boundary 2026-06-02 is the regime cut; see research/lean/README §3).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from research.eval.reproduce_v1 import (
    build_v1_run_spec,
    crosscheck_entries,
    load_oracle,
    parse_v1_decisions_from_log,
)
from research.lean import images
from research.lean.driver import DEFAULT_LEAN_LOCAL_IMAGE, run_backtest

pytestmark = pytest.mark.integration

_DATA_ROOT = Path(os.environ.get("RESEARCH_DATA_ROOT", "research/data/cache/lean_bars"))
_ORACLE_PATH = Path(os.environ.get("RESEARCH_V1_ORACLE", ""))
# Default 0.0: the meaningful gate is "reproduced >=1 oracle decision" (directional
# proof). An exact match is structurally limited (measured PR D: bar data revised
# since live decided, the pre-2026-06-02 ER regime flip, live pre-#312 re-emission
# dups, sizing-to-zero re-emits — and prod stamps a distinct param-hash per signal),
# so a high bar would be brittle. Operators can raise it via
# $RESEARCH_V1_MIN_MATCH_RATE.
_MIN_MATCH_RATE = float(os.environ.get("RESEARCH_V1_MIN_MATCH_RATE", "0.0"))


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
            "set $RESEARCH_V1_WINDOW=YYYY-MM-DD:YYYY-MM-DD to a single-REGIME window "
            "(cut by DATE — prod stamps a distinct parameter_set_hash per signal, so "
            "hash-filtering is unusable; the live regime boundary is the ER gate "
            "landing 2026-06-02). See research/lean/README → reproduce-V1."
        )

    spec = build_v1_run_spec(_DATA_ROOT)
    run_backtest(spec, work_dir=tmp_path, backend="docker")  # produces the output dir + log
    # V1 emits decisions via POST + places no LEAN orders, so its decisions are in the
    # LEAN log, not the result JSON.
    entries = parse_v1_decisions_from_log(tmp_path / "output")
    assert entries, "V1 backtest produced no entry decisions — check warmup/data depth"
    oracle = load_oracle(_ORACLE_PATH)
    report = crosscheck_entries(entries, oracle, window=window)
    # Directional proof: the harness drives prod V1 end-to-end and reproduces at least
    # one live decision in-window. Exact reproduction is structurally limited (above).
    assert report.matched, f"no oracle decisions reproduced in-window: {report.summary()}"
    assert report.match_rate >= _MIN_MATCH_RATE, report.summary()
