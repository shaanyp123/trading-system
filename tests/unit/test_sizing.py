"""Unit tests for services/risk/sizing.py — Stages 0-5.

Test inventory derived from backend-spec §10.1 row "services/risk/sizing.py":
- Stage 0 universe filter at $15k / $25k / $50k / $100k tiers
- Stage 2 50%-override for /MES at $20k
- Stage 3 cluster shrink convergence (<=10 iter)
- Stage 3 non-convergence drops lowest-momentum signal and restarts
- Stage 5 lot rounding sub_minimum_size detection

Plus pipeline-integration cases covering the public ``run_sizing_pipeline``
contract: invariant violations, idempotence on stable inputs, audit event
flushing.

Test naming follows claude-dev-guide §6.1
(``test_<unit>_<scenario>_<expected>``).

Per anti-pattern A22, no audit events are written here — the pipeline is a
pure function and ``pending_audit_events`` is asserted as data on the
``SizingResult``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from services.risk.sizing import (
    DEFAULT_HARD_FLOOR_PCT,
    DEFAULT_TARGET_CAP_PCT,
    ContractMetadata,
    SizingError,
    SizingInputs,
    _nearest_psd,
    _stage_0_universe_filter,
    _stage_1_inverse_vol,
    _stage_2_per_position_cap,
    _stage_3_cluster_shrink,
    _stage_5_lot_rounding,
    run_sizing_pipeline,
)
from strategies.v1_trend_following.signals import CandidateSignal, Direction

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Single-contract notionals matching parameters.py comments (V1 candidate
#: universe, lock 2026-05-05). Approximate "current price x multiplier" for
#: micro futures and per-share price for ETFs.
V1_NOTIONALS: Mapping[str, Decimal] = {
    "/MES": Decimal("26000"),  # 5235 x 5
    "/MNQ": Decimal("36000"),  # 18000 x 2
    "/MYM": Decimal("20000"),  # 40000 x 0.50
    "/M2K": Decimal("10000"),  # 2000 x 5
    "/MGC": Decimal("24000"),  # 2400 x 10
    "/MCL": Decimal("8000"),  # 80 x 100
    "/MBT": Decimal("10000"),  # 100000 x 0.1
    "TLT": Decimal("90"),
    "IEF": Decimal("90"),
    "SHY": Decimal("80"),
    "TIP": Decimal("100"),
}

V1_CLUSTERS: Mapping[str, str] = {
    "/MES": "equity_index",
    "/MNQ": "equity_index",
    "/MYM": "equity_index",
    "/M2K": "equity_index",
    "/MGC": "commodity",
    "/MCL": "commodity",
    "/MBT": "crypto",
    "TLT": "rates",
    "IEF": "rates",
    "SHY": "rates",
    "TIP": "rates",
}


def _v1_metadata() -> dict[str, ContractMetadata]:
    return {
        market: ContractMetadata(
            market=market,
            single_contract_notional=V1_NOTIONALS[market],
            cluster=V1_CLUSTERS[market],
            min_lot=1,
        )
        for market in V1_NOTIONALS
    }


def _make_candidate(
    market: str, *, direction: Direction = Direction.LONG, decision_price: Decimal | None = None
) -> CandidateSignal:
    return CandidateSignal(
        market=market,
        direction=direction,
        signal_type="donchian_breakout",
        session_date=date(2026, 5, 6),
        decision_price=decision_price or V1_NOTIONALS[market],
        stop_price=V1_NOTIONALS[market] * Decimal("0.97"),
        indicators_snapshot={},
    )


def _diag_sigma(markets: list[str], sigma_per_market: list[float]) -> np.ndarray:
    assert len(markets) == len(sigma_per_market)
    sigma_vec = np.array(sigma_per_market, dtype=np.float64)
    return np.diag(sigma_vec**2)  # diagonal covariance: variance = sigma**2


def _correlated_sigma(markets: list[str], sigma_per_market: list[float], rho: float) -> np.ndarray:
    assert len(markets) == len(sigma_per_market)
    n = len(markets)
    sigma_vec = np.array(sigma_per_market, dtype=np.float64)
    corr = np.full((n, n), rho)
    np.fill_diagonal(corr, 1.0)
    sigma: np.ndarray = np.outer(sigma_vec, sigma_vec) * corr
    return sigma


# ===========================================================================
# Stage 0 — universe filter
# ===========================================================================


class TestStage0UniverseFilter:
    """Backend-spec §2.4.1 Stage 0: 1-contract notional <= 50% x equity."""

    def test_stage_0_15k_excludes_all_micros_admits_etfs(self) -> None:
        """At $15k equity, threshold is $7.5k — every V1 micro future exceeds."""
        meta = _v1_metadata()
        s0 = _stage_0_universe_filter(
            candidate_markets=tuple(V1_NOTIONALS.keys()),
            equity=Decimal("15000"),
            contracts_metadata=meta,
            single_contract_overrides={},
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        assert set(s0.active_markets) == {"TLT", "IEF", "SHY", "TIP"}
        excluded_markets = {e["market"] for e in s0.excluded}
        assert excluded_markets == {"/MES", "/MNQ", "/MYM", "/M2K", "/MGC", "/MCL", "/MBT"}

    def test_stage_0_25k_admits_8k_to_12k_micros(self) -> None:
        """At $25k equity, threshold is $12.5k — admits /MCL ($8k), /M2K ($10k),
        /MBT ($10k) plus the four ETFs."""
        meta = _v1_metadata()
        s0 = _stage_0_universe_filter(
            candidate_markets=tuple(V1_NOTIONALS.keys()),
            equity=Decimal("25000"),
            contracts_metadata=meta,
            single_contract_overrides={},
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        assert set(s0.active_markets) == {
            "/MCL",
            "/M2K",
            "/MBT",
            "TLT",
            "IEF",
            "SHY",
            "TIP",
        }
        assert {e["market"] for e in s0.excluded} == {"/MES", "/MNQ", "/MYM", "/MGC"}

    def test_stage_0_50k_admits_through_24k_excludes_mes_mnq(self) -> None:
        """At $50k equity, threshold is $25k — admits up through /MGC ($24k);
        excludes /MES ($26k) and /MNQ ($36k)."""
        meta = _v1_metadata()
        s0 = _stage_0_universe_filter(
            candidate_markets=tuple(V1_NOTIONALS.keys()),
            equity=Decimal("50000"),
            contracts_metadata=meta,
            single_contract_overrides={},
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        assert {"/MES", "/MNQ"} == {e["market"] for e in s0.excluded}
        for admitted in ("/MYM", "/M2K", "/MGC", "/MCL", "/MBT", "TLT", "IEF", "SHY", "TIP"):
            assert admitted in s0.active_markets

    def test_stage_0_100k_admits_all_v1_candidates(self) -> None:
        """At $100k equity, threshold is $50k — every V1 candidate qualifies."""
        meta = _v1_metadata()
        s0 = _stage_0_universe_filter(
            candidate_markets=tuple(V1_NOTIONALS.keys()),
            equity=Decimal("100000"),
            contracts_metadata=meta,
            single_contract_overrides={},
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        assert set(s0.active_markets) == set(V1_NOTIONALS.keys())
        assert s0.excluded == ()

    def test_stage_0_emits_universe_exclusion_audit_per_dropped_market(self) -> None:
        """Each excluded market produces one ``universe_exclusion`` audit event
        with the canonical payload shape (backend-spec §3.30)."""
        meta = _v1_metadata()
        s0 = _stage_0_universe_filter(
            candidate_markets=tuple(V1_NOTIONALS.keys()),
            equity=Decimal("15000"),
            contracts_metadata=meta,
            single_contract_overrides={},
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        assert len(s0.audit_events) == 7  # all micros excluded at $15k
        for ev in s0.audit_events:
            assert ev.event_type == "universe_exclusion"
            assert ev.payload["exclusion_reason"] == (
                "single_contract_notional_exceeds_50pct_equity"
            )
            assert ev.payload["evaluated_at_utc"] == "2026-05-06T17:30:00Z"

    def test_stage_0_zero_equity_raises_sizing_error(self) -> None:
        meta = _v1_metadata()
        with pytest.raises(SizingError, match="equity must be positive"):
            _stage_0_universe_filter(
                candidate_markets=("/MCL",),
                equity=Decimal("0"),
                contracts_metadata=meta,
                single_contract_overrides={},
                evaluated_at_utc="",
            )

    def test_stage_0_missing_metadata_raises_sizing_error(self) -> None:
        with pytest.raises(SizingError, match="missing ContractMetadata"):
            _stage_0_universe_filter(
                candidate_markets=("/UNKNOWN",),
                equity=Decimal("100000"),
                contracts_metadata={},
                single_contract_overrides={},
                evaluated_at_utc="",
            )


# ===========================================================================
# Stage 2 — per-position cap with single-contract overrides
# ===========================================================================


class TestStage2OverrideMesAt20k:
    """Backend-spec §2.4.1 Stage 2: 25% target weight / 50% override for /MES
    at $20k+ (per parameters.py comment)."""

    def test_stage_0_admits_mes_at_20k_when_override_active(self) -> None:
        """The override map carries /MES → $20k; at exactly $20k equity, /MES
        is admitted via the override path even though $26k > 50% x $20k = $10k."""
        meta = _v1_metadata()
        s0 = _stage_0_universe_filter(
            candidate_markets=("/MES",),
            equity=Decimal("20000"),
            contracts_metadata=meta,
            single_contract_overrides={"/MES": Decimal("20000")},
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        assert "/MES" in s0.active_markets
        assert s0.overrides_applied == ("/MES",)
        assert s0.excluded == ()
        assert s0.audit_events == ()

    def test_stage_2_override_market_uses_50pct_cap_not_25pct(self) -> None:
        """A market in ``overrides_applied`` gets ``hard_floor_pct x equity``
        (50% = $10k at $20k equity); not the 25% default ($5k)."""
        equity = Decimal("20000")
        s2 = _stage_2_per_position_cap(
            unconstrained_notional={"/MES": Decimal("18000")},  # > both caps
            equity=equity,
            target_cap_pct=DEFAULT_TARGET_CAP_PCT,  # 0.25
            hard_floor_pct=DEFAULT_HARD_FLOOR_PCT,  # 0.50
            overrides_applied=("/MES",),
        )
        assert s2.capped_notional["/MES"] == Decimal("10000")  # 0.50 x 20000

    def test_stage_2_non_override_market_uses_25pct_cap(self) -> None:
        """Markets NOT in overrides_applied get the 25% default cap."""
        equity = Decimal("20000")
        s2 = _stage_2_per_position_cap(
            unconstrained_notional={"/MCL": Decimal("18000")},
            equity=equity,
            target_cap_pct=DEFAULT_TARGET_CAP_PCT,
            hard_floor_pct=DEFAULT_HARD_FLOOR_PCT,
            overrides_applied=(),
        )
        assert s2.capped_notional["/MCL"] == Decimal("5000")  # 0.25 x 20000

    def test_stage_0_below_override_threshold_excludes_mes(self) -> None:
        """At equity = $19,999, the override threshold ($20k) does NOT activate;
        /MES is excluded by the standard Stage 0 rule."""
        meta = _v1_metadata()
        s0 = _stage_0_universe_filter(
            candidate_markets=("/MES",),
            equity=Decimal("19999"),
            contracts_metadata=meta,
            single_contract_overrides={"/MES": Decimal("20000")},
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        assert "/MES" not in s0.active_markets
        assert s0.overrides_applied == ()
        assert len(s0.audit_events) == 1

    def test_stage_2_caps_propagate_into_trace(self) -> None:
        """The Stage 2 trace records overrides_applied for downstream auditability."""
        s2 = _stage_2_per_position_cap(
            unconstrained_notional={"/MES": Decimal("12000"), "/MCL": Decimal("3000")},
            equity=Decimal("20000"),
            target_cap_pct=DEFAULT_TARGET_CAP_PCT,
            hard_floor_pct=DEFAULT_HARD_FLOOR_PCT,
            overrides_applied=("/MES",),
        )
        assert s2.trace["single_contract_overrides_applied"] == ["/MES"]
        assert s2.trace["target_cap_pct"] == "0.25"
        assert s2.trace["hard_floor_pct"] == "0.50"


# ===========================================================================
# Stage 3 — cluster shrink (convergence + non-convergence + PSD repair)
# ===========================================================================


class TestStage3ClusterShrink:
    """Backend-spec §2.4.1 Stage 3: iterative cluster scaling, <=10 iterations,
    0.1% tolerance, drop-lowest-momentum on non-convergence."""

    def test_stage_3_no_breach_passes_in_one_iteration(self) -> None:
        """When all clusters are within cluster_cap, one iteration converges."""
        meta = _v1_metadata()
        s3 = _stage_3_cluster_shrink(
            capped_notional={"/MCL": Decimal("3000"), "/MGC": Decimal("4000")},
            contracts_metadata=meta,
            psd_repair_was_applied=False,
            cluster_cap_pct=Decimal("1.00"),
            equity=Decimal("100000"),
            momentum_z_scores={"/MCL": Decimal("1"), "/MGC": Decimal("0.5")},
        )
        assert s3.trace["convergence_tolerance_met"] is True
        assert s3.trace["scaled_clusters"] == []
        assert s3.dropped_markets == ()
        assert s3.scaled_notional == {"/MCL": Decimal("3000"), "/MGC": Decimal("4000")}

    def test_stage_3_breach_scales_cluster_proportionally(self) -> None:
        """A cluster breaching the cap is uniformly scaled; ratio <= cluster_cap."""
        meta = _v1_metadata()
        # Total commodity notional = $30k; cap at 25% x $50k equity = $12.5k.
        s3 = _stage_3_cluster_shrink(
            capped_notional={"/MCL": Decimal("12000"), "/MGC": Decimal("18000")},
            contracts_metadata=meta,
            psd_repair_was_applied=False,
            cluster_cap_pct=Decimal("0.25"),
            equity=Decimal("50000"),
            momentum_z_scores={"/MCL": Decimal("1"), "/MGC": Decimal("0.5")},
        )
        assert s3.trace["convergence_tolerance_met"] is True
        assert s3.trace["scaled_clusters"] == ["commodity"]
        total = sum(s3.scaled_notional.values(), Decimal("0"))
        assert abs(total - Decimal("12500")) < Decimal("1")  # within rounding tolerance

    def test_stage_3_non_convergence_drops_lowest_momentum_market(self) -> None:
        """Force non-convergence via ``max_iter=0``: the inner loop runs zero
        passes, the breach persists, and the outer drop-lowest-momentum-and-
        restart path fires. /MCL has the lower z-score → dropped first.

        The ``max_iter`` parameter exists specifically to make this branch
        testable; in production the constant ``CLUSTER_SHRINK_MAX_ITER`` (10)
        is used. With exact Decimal arithmetic and disjoint clusters, the
        inner loop converges in 1 iteration in normal scenarios; the drop
        path is a safety net for adversarial/bug cases."""
        meta = _v1_metadata()
        s3 = _stage_3_cluster_shrink(
            capped_notional={"/MCL": Decimal("3000"), "/MGC": Decimal("5000")},
            contracts_metadata=meta,
            psd_repair_was_applied=False,
            cluster_cap_pct=Decimal("0.10"),  # cap = $5k at $50k equity
            equity=Decimal("50000"),
            momentum_z_scores={"/MCL": Decimal("-0.5"), "/MGC": Decimal("1.5")},
            max_iter=0,
        )
        # /MCL has the lowest z-score → dropped FIRST.
        assert s3.dropped_markets[0] == "/MCL"
        # After /MCL is dropped, /MGC alone = $5000; cluster_cap = $5000 →
        # no breach; converges. /MGC survives.
        assert "/MGC" in s3.scaled_notional

    def test_stage_3_zero_cluster_cap_drops_until_empty(self) -> None:
        """``cluster_cap_pct=0`` makes every cluster permanently over-cap.
        Drops cascade: lowest momentum first, then next, etc., until empty."""
        meta = _v1_metadata()
        s3 = _stage_3_cluster_shrink(
            capped_notional={"/MCL": Decimal("3000"), "/MGC": Decimal("5000")},
            contracts_metadata=meta,
            psd_repair_was_applied=False,
            cluster_cap_pct=Decimal("0"),
            equity=Decimal("50000"),
            momentum_z_scores={"/MCL": Decimal("-0.5"), "/MGC": Decimal("1.5")},
            max_iter=0,
        )
        # Both dropped — /MCL first (lowest z), then /MGC.
        assert s3.dropped_markets[0] == "/MCL"
        assert "/MGC" in s3.dropped_markets
        assert s3.scaled_notional == {}

    def test_stage_3_psd_flag_propagates_to_trace(self) -> None:
        """The orchestrator computes ``psd_repair_was_applied``; Stage 3
        records it in its trace verbatim (no recomputation)."""
        meta = _v1_metadata()
        s3 = _stage_3_cluster_shrink(
            capped_notional={"/MCL": Decimal("1000"), "/MGC": Decimal("1000")},
            contracts_metadata=meta,
            psd_repair_was_applied=True,
            cluster_cap_pct=Decimal("1.00"),
            equity=Decimal("50000"),
            momentum_z_scores={"/MCL": Decimal("1"), "/MGC": Decimal("1")},
        )
        assert s3.trace["psd_repair_applied"] is True
        # Stage 3 itself emits no audit events — orchestrator owns that.
        assert s3.audit_events == ()


# ===========================================================================
# Stage 5 — lot rounding + sub-minimum drops
# ===========================================================================


class TestStage5LotRounding:
    """Backend-spec §2.4.1 Stage 5: banker's rounding; sub_minimum_size detection."""

    def test_stage_5_long_signal_produces_positive_count(self) -> None:
        s5 = _stage_5_lot_rounding(
            final_notional={"/MCL": Decimal("16000")},  # 2 contracts at $8k each
            contracts_metadata=_v1_metadata(),
            directions={"/MCL": Direction.LONG},
        )
        assert s5.target_contracts["/MCL"] == 2

    def test_stage_5_short_signal_produces_negative_count(self) -> None:
        s5 = _stage_5_lot_rounding(
            final_notional={"/MCL": Decimal("16000")},
            contracts_metadata=_v1_metadata(),
            directions={"/MCL": Direction.SHORT},
        )
        assert s5.target_contracts["/MCL"] == -2

    def test_stage_5_bankers_rounding_half_to_even(self) -> None:
        """ROUND_HALF_EVEN: 0.5 → 0; 1.5 → 2; 2.5 → 2; 3.5 → 4. Verify a 2.5
        case rounds DOWN to 2 (banker's even-tie behavior)."""
        meta = _v1_metadata()
        # 2.5 contracts of $8k notional → $20k.
        s5 = _stage_5_lot_rounding(
            final_notional={"/MCL": Decimal("20000")},
            contracts_metadata=meta,
            directions={"/MCL": Direction.LONG},
        )
        assert s5.target_contracts["/MCL"] == 2  # 2.5 rounds to even (2), not up

    def test_stage_5_sub_minimum_drops_zero_count_market(self) -> None:
        """A notional below 0.5 contracts rounds to 0 → tagged in
        ``sub_minimum_drops`` and absent from ``target_contracts``."""
        meta = _v1_metadata()
        # 0.4 contracts of $8k → $3,200.
        s5 = _stage_5_lot_rounding(
            final_notional={"/MCL": Decimal("3200")},
            contracts_metadata=meta,
            directions={"/MCL": Direction.LONG},
        )
        assert "/MCL" not in s5.target_contracts
        assert s5.trace["sub_minimum_drops"] == ["/MCL"]
        assert s5.trace["rounded"]["/MCL"] == 0

    def test_stage_5_records_pre_round_and_deviation_pct(self) -> None:
        """Trace surfaces the pre-round count and rounding deviation per market
        for operator-visible quality monitoring."""
        meta = _v1_metadata()
        s5 = _stage_5_lot_rounding(
            final_notional={"/MCL": Decimal("12000")},  # 1.5 → rounds to 2
            contracts_metadata=meta,
            directions={"/MCL": Direction.LONG},
        )
        # 1.5 → ROUND_HALF_EVEN → 2 (since 2 is even).
        assert s5.target_contracts["/MCL"] == 2
        assert s5.trace["contract_count_pre_round"]["/MCL"] == "1.5000"
        # |1.5 - 2| / 1.5 = 0.3333...
        assert s5.trace["rounding_deviation_pct"]["/MCL"] == "0.3333"

    def test_stage_5_zero_notional_drops_market(self) -> None:
        meta = _v1_metadata()
        s5 = _stage_5_lot_rounding(
            final_notional={"/MCL": Decimal("0")},
            contracts_metadata=meta,
            directions={"/MCL": Direction.LONG},
        )
        assert "/MCL" not in s5.target_contracts
        assert "/MCL" in s5.trace["sub_minimum_drops"]


# ===========================================================================
# Stage 1 — inverse-vol weighting (sanity coverage; full coverage in
# integration paths)
# ===========================================================================


class TestStage1InverseVol:
    def test_stage_1_two_market_diagonal_sigma_normalizes_weights_to_one(self) -> None:
        markets = ["/MCL", "/MGC"]
        sigma = _diag_sigma(markets, [0.30, 0.20])
        s1 = _stage_1_inverse_vol(
            active_markets=markets,
            equity=Decimal("100000"),
            sigma_annual_full=sigma,
            sigma_index=tuple(markets),
            vol_target_pct_annual=Decimal("0.15"),
            m_combined=Decimal("1.0"),
        )
        # raw_weight ratio = (1/0.20) / (1/0.30) = 1.5 → /MGC carries 60%, /MCL 40%
        w_mcl = Decimal(s1.trace["unconstrained_weight"]["/MCL"])
        w_mgc = Decimal(s1.trace["unconstrained_weight"]["/MGC"])
        assert abs(w_mcl + w_mgc - Decimal("1")) < Decimal("0.001")
        assert w_mgc > w_mcl  # lower vol → higher weight

    def test_stage_1_empty_active_markets_returns_empty_trace(self) -> None:
        sigma = np.zeros((1, 1))
        s1 = _stage_1_inverse_vol(
            active_markets=[],
            equity=Decimal("100000"),
            sigma_annual_full=sigma,
            sigma_index=("/MCL",),
            vol_target_pct_annual=Decimal("0.15"),
            m_combined=Decimal("1.0"),
        )
        assert s1.unconstrained_notional == {}
        assert s1.trace["unconstrained_notional"] == {}

    def test_stage_1_m_combined_halves_target_vol(self) -> None:
        """At m_combined=0.5 (e.g. CONVALESCENT), targeting 0.15 annual becomes
        0.075 effective; portfolio leverage scales proportionally lower."""
        markets = ["/MCL", "/MGC"]
        sigma = _diag_sigma(markets, [0.30, 0.20])
        s_full = _stage_1_inverse_vol(
            active_markets=markets,
            equity=Decimal("100000"),
            sigma_annual_full=sigma,
            sigma_index=tuple(markets),
            vol_target_pct_annual=Decimal("0.15"),
            m_combined=Decimal("1.0"),
        )
        s_half = _stage_1_inverse_vol(
            active_markets=markets,
            equity=Decimal("100000"),
            sigma_annual_full=sigma,
            sigma_index=tuple(markets),
            vol_target_pct_annual=Decimal("0.15"),
            m_combined=Decimal("0.5"),
        )
        # Halving m_combined halves notionals (linear scaling).
        for market in markets:
            full = s_full.unconstrained_notional[market]
            half = s_half.unconstrained_notional[market]
            ratio = half / full
            assert abs(ratio - Decimal("0.5")) < Decimal("0.001")

    def test_stage_1_degenerate_sigma_zero_variance_raises(self) -> None:
        markets = ["/MCL"]
        sigma_bad = np.zeros((1, 1))  # zero variance
        with pytest.raises(SizingError, match="non-positive variance"):
            _stage_1_inverse_vol(
                active_markets=markets,
                equity=Decimal("100000"),
                sigma_annual_full=sigma_bad,
                sigma_index=tuple(markets),
                vol_target_pct_annual=Decimal("0.15"),
                m_combined=Decimal("1.0"),
            )


# ===========================================================================
# PSD repair helper — sanity
# ===========================================================================


class TestNearestPSD:
    def test_nearest_psd_already_psd_returns_unchanged(self) -> None:
        sigma = np.array([[1.0, 0.3], [0.3, 1.0]])
        repaired, was_repaired, eig_pre, _eig_post = _nearest_psd(sigma)
        assert was_repaired is False
        assert np.array_equal(repaired, sigma)
        assert eig_pre > 0

    def test_nearest_psd_clamps_negative_eigenvalue(self) -> None:
        sigma = np.array([[1.0, 1.5], [1.5, 1.0]])  # not PSD; eigvals 2.5, -0.5
        _repaired, was_repaired, eig_pre, eig_post = _nearest_psd(sigma)
        assert was_repaired is True
        assert eig_pre < 0
        assert eig_post >= -1e-9  # ~zero after clamp


# ===========================================================================
# Pipeline integration
# ===========================================================================


class TestRunSizingPipeline:
    def test_pipeline_realistic_input_produces_target_contracts(self) -> None:
        """End-to-end: 3 candidates at $50k equity → contracts produced for
        admitted markets, audit events queued."""
        markets_in_index = ["/MCL", "/MGC", "TLT"]
        sigma = _correlated_sigma(markets_in_index, [0.30, 0.20, 0.10], rho=0.20)
        inputs = SizingInputs(
            candidate_signals=(
                _make_candidate("/MCL"),
                _make_candidate("/MGC"),
                _make_candidate("TLT"),
            ),
            equity=Decimal("50000"),
            contracts_metadata=_v1_metadata(),
            sigma_annual=sigma,
            sigma_index=tuple(markets_in_index),
            momentum_z_scores={"/MCL": Decimal("1.5"), "/MGC": Decimal("0.5"), "TLT": Decimal("0")},
            vol_target_pct_annual=Decimal("0.15"),
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        result = run_sizing_pipeline(inputs)
        # All three pass Stage 0 at $50k. At least one survives to Stage 5.
        assert len(result.target_contracts) >= 1
        # Trace shape complete.
        assert result.sizing_trace["schema_version"] == 1
        assert "stage_0_universe" in result.sizing_trace
        assert "stage_5_lot_rounding" in result.sizing_trace

    def test_pipeline_negative_equity_raises_sizing_error(self) -> None:
        markets_in_index = ["/MCL"]
        sigma = _diag_sigma(markets_in_index, [0.30])
        inputs = SizingInputs(
            candidate_signals=(_make_candidate("/MCL"),),
            equity=Decimal("-100"),
            contracts_metadata=_v1_metadata(),
            sigma_annual=sigma,
            sigma_index=tuple(markets_in_index),
            momentum_z_scores={"/MCL": Decimal("0")},
            vol_target_pct_annual=Decimal("0.15"),
        )
        with pytest.raises(SizingError, match="equity must be positive"):
            run_sizing_pipeline(inputs)

    def test_pipeline_sigma_index_size_mismatch_raises_sizing_error(self) -> None:
        markets_in_index = ["/MCL", "/MGC"]
        sigma_too_small = np.eye(1)  # 1x1 but index has 2 markets
        inputs = SizingInputs(
            candidate_signals=(_make_candidate("/MCL"),),
            equity=Decimal("50000"),
            contracts_metadata=_v1_metadata(),
            sigma_annual=sigma_too_small,
            sigma_index=tuple(markets_in_index),
            momentum_z_scores={"/MCL": Decimal("0")},
            vol_target_pct_annual=Decimal("0.15"),
        )
        with pytest.raises(SizingError, match="sigma_annual shape"):
            run_sizing_pipeline(inputs)

    def test_pipeline_excluded_market_absent_from_target_contracts(self) -> None:
        """A market dropped at Stage 0 doesn't appear in target_contracts."""
        markets_in_index = ["/MNQ", "/MCL"]
        sigma = _diag_sigma(markets_in_index, [0.25, 0.30])
        inputs = SizingInputs(
            # /MNQ excluded at $15k; /MCL excluded at $15k too ($8k > $7.5k);
            # so target_contracts is empty.
            candidate_signals=(_make_candidate("/MNQ"), _make_candidate("/MCL")),
            equity=Decimal("15000"),
            contracts_metadata=_v1_metadata(),
            sigma_annual=sigma,
            sigma_index=tuple(markets_in_index),
            momentum_z_scores={"/MNQ": Decimal("1"), "/MCL": Decimal("0")},
            vol_target_pct_annual=Decimal("0.15"),
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        result = run_sizing_pipeline(inputs)
        assert "/MNQ" not in result.target_contracts
        assert "/MCL" not in result.target_contracts
        assert len(result.pending_audit_events) == 2  # universe_exclusion x 2

    def test_pipeline_short_direction_target_is_negative(self) -> None:
        markets_in_index = ["/MCL"]
        sigma = _diag_sigma(markets_in_index, [0.30])
        inputs = SizingInputs(
            candidate_signals=(_make_candidate("/MCL", direction=Direction.SHORT),),
            equity=Decimal("100000"),
            contracts_metadata=_v1_metadata(),
            sigma_annual=sigma,
            sigma_index=tuple(markets_in_index),
            momentum_z_scores={"/MCL": Decimal("0")},
            vol_target_pct_annual=Decimal("0.15"),
        )
        result = run_sizing_pipeline(inputs)
        assert "/MCL" in result.target_contracts
        assert result.target_contracts["/MCL"] < 0  # SHORT → negative

    def test_pipeline_psd_repair_event_propagates_to_pending_audit_events(self) -> None:
        markets_in_index = ["/MCL", "/MGC"]
        sigma_bad = _correlated_sigma(markets_in_index, [0.30, 0.30], rho=1.5)
        inputs = SizingInputs(
            candidate_signals=(_make_candidate("/MCL"), _make_candidate("/MGC")),
            equity=Decimal("100000"),
            contracts_metadata=_v1_metadata(),
            sigma_annual=sigma_bad,
            sigma_index=tuple(markets_in_index),
            momentum_z_scores={"/MCL": Decimal("0"), "/MGC": Decimal("0")},
            vol_target_pct_annual=Decimal("0.15"),
            evaluated_at_utc="2026-05-06T17:30:00Z",
        )
        result = run_sizing_pipeline(inputs)
        psd_events = [
            ev for ev in result.pending_audit_events if ev.event_type == "psd_repair_applied"
        ]
        assert len(psd_events) == 1
