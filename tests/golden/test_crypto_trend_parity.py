"""Golden: crypto-perps signal-engine parity vs the reference backtest (C0 exit gate).

Delta spec §5 C0 offline gate + §3.3: running the production signal
engine (``services/signal/crypto_trend.py``) over the FULL recorded
2016-2026 bar history (``research/crypto_perps/data/*.csv``) must
produce results IDENTICAL to the reference implementation
(``research/crypto_perps/backtest.py`` — the locked backtest authority,
delta spec §4/§6). This is the same trust-bridge discipline the LEAN
stack had, applied to the crypto engine.

What "identical" means here — asserted EXACTLY, not approximately:

* the trade list matches tuple-for-tuple: same dates, same assets,
  same signed integer contract deltas ("identical integer-contract
  targets"), same fill prices, same float costs, same reasons;
* final equity matches bit-for-bit (float ``==``) — any arithmetic
  divergence anywhere in the loop shows up here;
* both the strategy-doc BASE profile and the operator-locked
  PRODUCTION profile (Amendment B) are gated.

The engine deliberately reuses the same pandas indicator calls as the
reference (see crypto_trend module docstring), so a parity failure
means the DECISION port drifted — exactly what this gate exists to
catch. If this test fails after an intentional strategy change, the
change is by definition an amendment: reference first, decisions-log
entry, then engine.

Runs in CI (pandas is a runtime dependency as of C0-B3). ~2s per
profile on the 3.4k-day history.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.golden

pd = pytest.importorskip("pandas", reason="pandas required for the parity gate")

from research.crypto_perps import backtest as reference  # noqa: E402
from services.signal import crypto_trend as engine  # noqa: E402

#: The reference's canonical evaluation window (backtest.py CLI defaults).
#: Bars BEFORE the start date still feed indicator warm-up in both
#: implementations because indicators are computed on the full loaded
#: history prior to windowing — i.e. the full recorded 2016-2026 file
#: set is exercised.
START = "2017-01-01"
END = "2026-06-30"


def _load_reference_data(params: reference.Params) -> dict[str, Any]:
    return {
        s: reference.compute_indicators(reference.load_asset(s), params) for s in reference.ASSETS
    }


def _engine_params_from(params: reference.Params) -> engine.CryptoTrendParams:
    """Build engine params field-for-field from a reference Params.

    Constructed via the shared field names so a future field added to
    one dataclass but not the other fails loudly here instead of
    silently diverging.
    """
    ref_fields = {f: getattr(params, f) for f in params.__dataclass_fields__}
    eng_fields = set(engine.CryptoTrendParams.__dataclass_fields__)
    assert set(ref_fields) == eng_fields, (
        f"Params field drift: reference-only={set(ref_fields) - eng_fields}, "
        f"engine-only={eng_fields - set(ref_fields)}"
    )
    return engine.CryptoTrendParams(**ref_fields)


def _reference_production_params() -> reference.Params:
    """The reference `production` scenario (Amendment B), built exactly as
    backtest.main() builds it."""
    from dataclasses import replace

    base = reference.Params()
    aggr = replace(
        base,
        v_target=0.80,
        per_trade_risk_frac=0.05,
        daily_loss_limit=-0.08,
        weekly_loss_limit=-0.16,
        gross_cap=3.0,
        dd_tiers=((0.20, 1.0), (0.40, 0.6), (0.70, 0.35), (9.9, 0.2)),
        halt_frac=0.25,
    )
    return replace(
        aggr,
        dd_tiers=((9.9, 1.0),),
        hysteresis_hold=True,
        band_edge_rebalance=True,
        cash_yield_ann=0.035,
        subscription_usd_month=4.99,
    )


class TestSignalEngineParity:
    """The C0-B3 exit gate: identical integer-contract targets, full history."""

    @pytest.mark.parametrize(
        ("profile", "ref_params_factory"),
        [
            ("base", reference.Params),
            ("production_amendment_b", _reference_production_params),
        ],
    )
    def test_full_history_parity(self, profile: str, ref_params_factory: Any) -> None:
        ref_params = ref_params_factory()
        eng_params = _engine_params_from(ref_params)

        # Amendment B lock: the engine's shipped production constant must
        # BE the reference production scenario.
        if profile == "production_amendment_b":
            assert eng_params == engine.AMENDMENT_B_PARAMS

        # Indicators: same loader, both compute_indicators implementations.
        raw = {s: reference.load_asset(s) for s in reference.ASSETS}
        ref_data = {s: reference.compute_indicators(raw[s], ref_params) for s in reference.ASSETS}
        eng_data = {s: engine.compute_indicators(raw[s], eng_params) for s in engine.ASSETS}
        for s in reference.ASSETS:
            pd.testing.assert_frame_equal(ref_data[s], eng_data[s], check_exact=True)

        # Full-loop replay: reference aggregate results vs engine simulate.
        ref_result = reference.run_backtest(ref_data, ref_params, START, END)
        eng_result = engine.simulate(eng_data, eng_params, START, END)

        # Trade-count surface (per-reason) — the reference exposes counts.
        eng_reasons: dict[str, int] = {}
        for t in eng_result.trades:
            eng_reasons[t.reason] = eng_reasons.get(t.reason, 0) + 1
        assert eng_reasons == ref_result["trade_reasons"], profile
        assert len(eng_result.trades) == ref_result["n_position_changes"], profile

        # Money surfaces — bit-exact float equality: any divergence in any
        # decision or accounting step over ~3.4k days lands here.
        assert eng_result.final_equity == pytest.approx(ref_result["final_equity"], abs=0.005), (
            profile
        )  # reference rounds to cents in summarize()
        assert round(eng_result.total_costs, 2) == ref_result["total_costs"], profile
        assert round(eng_result.total_funding, 2) == ref_result["total_funding_paid"], profile
        assert eng_result.halted == ref_result["halted"], profile

    @pytest.mark.parametrize(
        ("profile", "ref_params_factory"),
        [
            ("base", reference.Params),
            ("production_amendment_b", _reference_production_params),
        ],
    )
    def test_trade_level_parity(
        self, profile: str, ref_params_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tuple-for-tuple trade parity — the literal exit-gate assertion.

        The reference's ``run_backtest`` returns aggregates, so the raw
        trade list is captured by shimming ``reference.summarize`` for
        the duration of the run. Every (date, asset, delta, price, cost,
        reason) tuple must match the engine's exactly — this IS
        "identical integer-contract targets over the full recorded
        history".
        """
        ref_params = ref_params_factory()
        eng_params = _engine_params_from(ref_params)
        raw = {s: reference.load_asset(s) for s in reference.ASSETS}
        ref_data = {s: reference.compute_indicators(raw[s], ref_params) for s in reference.ASSETS}
        eng_data = {s: engine.compute_indicators(raw[s], eng_params) for s in engine.ASSETS}

        captured: dict[str, Any] = {}
        real_summarize = reference.summarize

        def _capturing_summarize(curve: Any, trades: Any, *args: Any, **kwargs: Any) -> Any:
            captured["trades"] = list(trades)
            captured["final_equity"] = float(curve.iloc[-1])
            return real_summarize(curve, trades, *args, **kwargs)

        monkeypatch.setattr(reference, "summarize", _capturing_summarize)
        reference.run_backtest(ref_data, ref_params, START, END)
        assert "trades" in captured, "summarize shim never fired"

        eng_result = engine.simulate(eng_data, eng_params, START, END)
        eng_tuples = [t.as_tuple() for t in eng_result.trades]
        assert eng_tuples == captured["trades"], profile
        # bit-exact equity: any arithmetic divergence anywhere lands here
        assert eng_result.final_equity == captured["final_equity"], profile

        # The "integer-contract targets" contract, explicitly.
        for t in eng_result.trades:
            assert t.delta == int(t.delta), t
        for _ts, targets in eng_result.daily_targets:
            for asset, target in targets.items():
                assert target == int(target), (asset, target)

    def test_engine_is_deterministic(self) -> None:
        ref_params = _reference_production_params()
        eng_params = _engine_params_from(ref_params)
        raw = {s: reference.load_asset(s) for s in reference.ASSETS}
        eng_data = {s: engine.compute_indicators(raw[s], eng_params) for s in engine.ASSETS}
        run1 = engine.simulate(eng_data, eng_params, START, END)
        run2 = engine.simulate(eng_data, eng_params, START, END)
        assert [t.as_tuple() for t in run1.trades] == [t.as_tuple() for t in run2.trades]
        assert run1.final_equity == run2.final_equity

    def test_results_json_pin_production(self) -> None:
        """Pin the engine against the COMMITTED reference results
        (research/crypto_perps/results.json, written by the reference run
        that validated Amendment B — decisions-log 2026-07-08 'final
        numbers'). Catches drift in EITHER implementation vs the numbers
        the operator approved."""
        import json
        from pathlib import Path

        results_path = Path(reference.RESULTS_PATH)
        if not results_path.exists():
            pytest.skip("results.json not committed")
        committed = json.loads(results_path.read_text())
        if "production" not in committed:
            pytest.skip("no production scenario in committed results")
        pinned = committed["production"]

        ref_params = _reference_production_params()
        eng_params = _engine_params_from(ref_params)
        raw = {s: reference.load_asset(s) for s in reference.ASSETS}
        eng_data = {s: engine.compute_indicators(raw[s], eng_params) for s in engine.ASSETS}
        result = engine.simulate(eng_data, eng_params, START, END)

        assert round(result.final_equity, 2) == pinned["final_equity"]
        assert len(result.trades) == pinned["n_position_changes"]
        eng_reasons: dict[str, int] = {}
        for t in result.trades:
            eng_reasons[t.reason] = eng_reasons.get(t.reason, 0) + 1
        assert eng_reasons == pinned["trade_reasons"]
        assert result.halted == pinned["halted"]
