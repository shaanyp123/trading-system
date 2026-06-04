"""§6.6 vbt/numpy ↔ LEAN parity rail — REAL (promoted from the dev-guide sketch).

Runs the SAME daily strategy (the Donchian reference) through BOTH the research
numpy evaluator and LEAN, then asserts the locked §6.6 tolerances:

* per-trade slippage diff ``<= 5 bps``
* aggregate P&L divergence ``<= 0.5%`` of starting equity
* trade-count divergence ``<= 5%``

This is the trust-bridge proof (design §8): if the fast screen and the engine of
record disagree beyond tolerance, a vbt result cannot inform a graduation decision.

**SKIP, never silent pass (design §9 P2 hard reality).** LEAN/Docker are absent in
CI and on most dev machines, and the data snapshot is gitignored. When either is
missing the test SKIPS with an actionable reason — a missing LEAN must be VISIBLE,
not a green check. The parity *logic* itself is unit-tested without LEAN in
``tests/unit/test_research_parity.py``; this test exercises it against a real engine.

Parity uses a bond ETF (single clean daily series, no contract rolls) so the only
LEAN-vs-numpy divergence is the fill convention (LEAN fills next-open; the numpy
screen is close-to-close) — exactly what the tolerances cover. Futures/continuous
parity is exercised separately by the reproduce-V1 run (design §8).
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from research.data.contract_specs import get_spec
from research.data.daily_loader import load_daily_series
from research.eval.parity import check_parity, extract_trades_from_positions, lean_trade_to_trade
from research.lean import images
from research.lean.config_render import reference_parameters
from research.lean.driver import DEFAULT_LEAN_LOCAL_IMAGE, LeanRunSpec, run_backtest
from research.screen.daily_eval import evaluate_daily
from research.strategy.donchian import DonchianBreakout

pytestmark = pytest.mark.integration

# Parity run knobs (overridable via env for the operator's real-run window).
_SYMBOL = os.environ.get("RESEARCH_PARITY_SYMBOL", "TLT")
_CHANNEL = int(os.environ.get("RESEARCH_PARITY_CHANNEL", "20"))
_CASH = int(os.environ.get("RESEARCH_PARITY_CASH", "100000"))
_START = date.fromisoformat(os.environ.get("RESEARCH_PARITY_START", "2024-01-01"))
_END = date.fromisoformat(os.environ.get("RESEARCH_PARITY_END", "2025-12-31"))
_DATA_ROOT = Path(os.environ.get("RESEARCH_DATA_ROOT", "research/data/cache/lean_bars"))
_REFERENCE_ALGO = Path("research/lean/projects/donchian_reference.py")


def test_vectorbt_lean_parity(tmp_path: Path) -> None:
    backend = images.runnable_backend(lean_local_image=DEFAULT_LEAN_LOCAL_IMAGE)
    if backend is None:
        pytest.skip(
            "no RUNNABLE LEAN backend — need either the `lean` CLI + Docker, or the "
            f"`{DEFAULT_LEAN_LOCAL_IMAGE}` image built + Docker "
            f"({images.availability_report()}). Build it per research/lean/README. "
            "(design §9 P2; never a silent pass)"
        )
    if not _DATA_ROOT.exists():
        pytest.skip(
            f"data snapshot absent at {_DATA_ROOT} — copy the on-disk LEAN bars first "
            "(research/README §Quickstart; NEVER the live trading_lean_data volume)"
        )

    spec = get_spec(_SYMBOL)
    multiplier = float(spec.multiplier)

    # --- research (numpy) side ------------------------------------------------
    series = load_daily_series(_DATA_ROOT, _SYMBOL, start=_START, end=_END)
    strategy = DonchianBreakout(channel=_CHANNEL, contracts=1)
    research_result = evaluate_daily(
        series, strategy, multiplier=multiplier, starting_cash=float(_CASH)
    )
    research_trades = extract_trades_from_positions(
        research_result.positions, series.close, multiplier
    )

    # --- LEAN (authority) side ------------------------------------------------
    run_spec = LeanRunSpec(
        algorithm_type_name="DonchianReferenceAlgorithm",
        algorithm_source=_REFERENCE_ALGO,
        parameters=reference_parameters(
            {"DONCHIAN_CHANNEL": _CHANNEL, "CONTRACTS": 1, "RESEARCH_SYMBOL": _SYMBOL},
            start=_START,
            end=_END,
            starting_cash=_CASH,
        ),
        data_root=_DATA_ROOT,
        symbol=_SYMBOL,
        multiplier=multiplier,
        strategy_name=strategy.name,
        starting_cash=float(_CASH),
    )
    parsed = run_backtest(run_spec, work_dir=tmp_path, backend=backend)
    lean_trades = [lean_trade_to_trade(t) for t in parsed.trades]

    # --- §6.6 verdict ---------------------------------------------------------
    verdict = check_parity(
        research_result,
        research_trades,
        parsed.result,
        lean_trades,
        starting_equity=Decimal(_CASH),
    )
    assert verdict.passed, verdict.summary()
