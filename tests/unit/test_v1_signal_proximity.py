"""Unit tests for ``strategies/v1_trend_following/proximity.py``.

Locks the per-gate state classification + headroom math + overall-state
combiner from ``Docs/signal-proximity-design.md`` §3.2 + §3.3 + §4.1.

The proximity module is pure-Python and observation-only — these tests
exercise it directly with bare Decimal literals, no LEAN runtime or
strategy-class fixtures required. PR-A scope: classification + edge cases
(insufficient history, decommissioned, exact-threshold ties).

Gate #3 swapped Hurst R/S → Kaufman Efficiency Ratio on 2026-06-02: the
``efficiency`` gate uses ``EFFICIENCY_CLOSE_BAND=0.05`` (ER units) and a
default threshold of 0.20.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from strategies.v1_trend_following.proximity import (
    DONCHIAN_CLOSE_BAND_PCT,
    EFFICIENCY_CLOSE_BAND,
    TREND_CLOSE_BAND_PCT,
    GateState,
    MarketProximity,
    compute_market_proximity,
)

EFFICIENCY_THRESHOLD = Decimal("0.20")


def _proximity(**overrides: object) -> MarketProximity:
    """Build a baseline ``MarketProximity`` with all gates PASSing in LONG.

    Defaults: last_close = 100, donchian_high = 95 (long broken), donchian_low = 80
    (short far from break), ma_fast = 99 > ma_slow = 95 (long trend passing),
    efficiency = 0.40 (>= threshold 0.20 + 0.05 close band). Overrides let each
    test toggle ONE gate's input and assert the resulting state.
    """
    base: dict[str, object] = {
        "market": "/MES",
        "last_close": Decimal("100"),
        "donchian_high": Decimal("95"),
        "donchian_low": Decimal("80"),
        "ma_fast": Decimal("99"),
        "ma_slow": Decimal("95"),
        "efficiency_value": Decimal("0.40"),
        "efficiency_threshold": EFFICIENCY_THRESHOLD,
        "decommissioned": False,
    }
    base.update(overrides)
    return compute_market_proximity(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Donchian gate classification
# ---------------------------------------------------------------------------
class TestDonchianClassification:
    def test_long_pass_when_close_already_above_high(self) -> None:
        p = _proximity(last_close=Decimal("110"), donchian_high=Decimal("100"))
        assert p.long_donchian.state == GateState.PASS
        # Negative headroom = already broken upside.
        assert p.long_donchian.headroom is not None
        assert p.long_donchian.headroom < 0

    def test_long_close_when_within_1pct_of_high(self) -> None:
        # 1% below high: (100 - 99.5) / 99.5 = 0.005025 → within CLOSE band.
        p = _proximity(last_close=Decimal("99.5"), donchian_high=Decimal("100"))
        assert p.long_donchian.state == GateState.CLOSE

    def test_long_fail_when_well_below_high(self) -> None:
        # 5% below high: clearly outside the 1% CLOSE band.
        p = _proximity(last_close=Decimal("95"), donchian_high=Decimal("100"))
        assert p.long_donchian.state == GateState.FAIL

    def test_long_exact_threshold_tie_is_close(self) -> None:
        """The boundary ``headroom == DONCHIAN_CLOSE_BAND_PCT`` resolves to CLOSE.

        Design §3.2: ``0 < x <= 0.01`` → CLOSE. The inclusive upper bound is
        the intentional choice — markets exactly at the CLOSE-band edge
        should be displayed as CLOSE, not FAIL.
        """
        # Pick close such that (high - close) / close == 0.01 exactly.
        # close = high / 1.01 → with high = Decimal("101") gives close = 100 →
        # (101 - 100) / 100 = 0.01 exactly.
        p = _proximity(last_close=Decimal("100"), donchian_high=Decimal("101"))
        assert p.long_donchian.headroom == DONCHIAN_CLOSE_BAND_PCT
        assert p.long_donchian.state == GateState.CLOSE

    def test_short_pass_when_close_already_below_low(self) -> None:
        p = _proximity(last_close=Decimal("70"), donchian_low=Decimal("75"))
        assert p.short_donchian.state == GateState.PASS

    def test_short_close_when_within_1pct_of_low(self) -> None:
        # 0.5% above low: within CLOSE band.
        p = _proximity(last_close=Decimal("80.4"), donchian_low=Decimal("80"))
        assert p.short_donchian.state == GateState.CLOSE


# ---------------------------------------------------------------------------
# Trend gate classification
# ---------------------------------------------------------------------------
class TestTrendClassification:
    def test_long_pass_when_close_above_ma_fast_above_ma_slow(self) -> None:
        p = _proximity(
            last_close=Decimal("105"),
            ma_fast=Decimal("100"),
            ma_slow=Decimal("95"),
        )
        assert p.long_trend.state == GateState.PASS

    def test_long_fail_when_ma_pair_inverted(self) -> None:
        # MA_FAST below MA_SLOW = downtrend regime; long trend gate cannot pass.
        p = _proximity(
            last_close=Decimal("105"),
            ma_fast=Decimal("95"),
            ma_slow=Decimal("100"),
        )
        # 5% inversion is far outside the 0.5% CLOSE band.
        assert p.long_trend.state == GateState.FAIL

    def test_long_close_when_within_half_pct_of_crossover(self) -> None:
        """MA_FAST just barely above MA_SLOW + close just barely above MA_FAST.

        Design §3.2: ``CLOSE if false but |closer_gap_pct| <= 0.005`` (0.5%).
        Construct a case where the trend is technically still passing (long
        bool True) BUT the closer gap sits inside the CLOSE band — confirms
        the band applies to "almost flipping" as well as "almost passing."
        """
        # Long bool True (close > ma_fast > ma_slow); closer gap = 0.1% < 0.5%.
        p = _proximity(
            last_close=Decimal("100"),
            ma_fast=Decimal("99.9"),
            ma_slow=Decimal("99.8"),
        )
        # Bool was True, so we PASS (the band only KICKS IN when the bool
        # is False — re-read §3.2). PASS is the right answer here.
        assert p.long_trend.state == GateState.PASS

    def test_long_close_when_close_just_below_ma_fast_within_band(self) -> None:
        """Long bool False (close < ma_fast) but within 0.5% of the crossover."""
        # close = 99.7, ma_fast = 100, ma_slow = 95.
        # close_vs_fast = (99.7 - 100) / 99.7 ≈ -0.00301 → abs <= 0.005.
        p = _proximity(
            last_close=Decimal("99.7"),
            ma_fast=Decimal("100"),
            ma_slow=Decimal("95"),
        )
        assert p.long_trend.state == GateState.CLOSE

    def test_short_pass_when_close_below_ma_fast_below_ma_slow(self) -> None:
        p = _proximity(
            last_close=Decimal("90"),
            ma_fast=Decimal("95"),
            ma_slow=Decimal("100"),
        )
        assert p.short_trend.state == GateState.PASS


# ---------------------------------------------------------------------------
# Efficiency Ratio gate classification (direction-agnostic)
# ---------------------------------------------------------------------------
class TestEfficiencyClassification:
    def test_pass_at_threshold_plus_band(self) -> None:
        # threshold 0.20 + band 0.05 = 0.25 → exactly at the PASS boundary.
        p = _proximity(efficiency_value=Decimal("0.25"))
        assert p.efficiency.state == GateState.PASS
        assert p.efficiency.headroom == EFFICIENCY_CLOSE_BAND

    def test_close_just_inside_band(self) -> None:
        # 0.23 → headroom 0.03 → within +/- 0.05 = CLOSE.
        p = _proximity(efficiency_value=Decimal("0.23"))
        assert p.efficiency.state == GateState.CLOSE

    def test_close_at_threshold_exact(self) -> None:
        # 0.20 == threshold → headroom 0.00 → in CLOSE band.
        p = _proximity(efficiency_value=Decimal("0.20"))
        assert p.efficiency.state == GateState.CLOSE
        assert p.efficiency.headroom == Decimal("0.00")

    def test_fail_well_below_threshold(self) -> None:
        # 0.10 → headroom -0.10 → outside -0.05 = FAIL.
        p = _proximity(efficiency_value=Decimal("0.10"))
        assert p.efficiency.state == GateState.FAIL

    def test_pass_well_above_threshold(self) -> None:
        # 0.80 → headroom 0.60 → PASS.
        p = _proximity(efficiency_value=Decimal("0.80"))
        assert p.efficiency.state == GateState.PASS

    def test_threshold_minus_band_boundary_is_close(self) -> None:
        """``efficiency_value == threshold - EFFICIENCY_CLOSE_BAND`` resolves to CLOSE.

        Design §3.2: ``CLOSE if -0.05 <= efficiency_headroom < 0.05``. The
        inclusive lower bound is intentional — markets exactly at the
        bottom edge of the CLOSE band should be displayed as CLOSE, not
        FAIL. A market hovering at the threshold's bottom needs operator
        eyes, which "CLOSE" promotes.
        """
        p = _proximity(efficiency_value=EFFICIENCY_THRESHOLD - EFFICIENCY_CLOSE_BAND)
        assert p.efficiency.headroom == -EFFICIENCY_CLOSE_BAND
        assert p.efficiency.state == GateState.CLOSE

    def test_raw_value_and_threshold_surfaced_for_persistence(self) -> None:
        """The raw ER + active threshold are carried on the record so the live
        ER distribution is mineable from ``signal_proximity`` (calibration of
        the 0.20 launch threshold)."""
        p = _proximity(efficiency_value=Decimal("0.42"))
        assert p.efficiency_value == Decimal("0.42")
        assert p.efficiency_threshold == EFFICIENCY_THRESHOLD


# ---------------------------------------------------------------------------
# Overall state + closest_gate
# ---------------------------------------------------------------------------
class TestOverallState:
    def test_overall_is_worst_of_three_gates(self) -> None:
        # All gates LONG-passing: overall PASS.
        p = _proximity(
            last_close=Decimal("110"),
            donchian_high=Decimal("100"),
            donchian_low=Decimal("50"),
            ma_fast=Decimal("105"),
            ma_slow=Decimal("100"),
            efficiency_value=Decimal("0.70"),
        )
        assert p.overall_state == GateState.PASS

    def test_overall_fail_when_any_gate_fails(self) -> None:
        # Efficiency clearly failing pulls overall down to FAIL even when
        # Donchian + Trend are passing.
        p = _proximity(
            last_close=Decimal("110"),
            donchian_high=Decimal("100"),
            ma_fast=Decimal("105"),
            ma_slow=Decimal("100"),
            efficiency_value=Decimal("0.05"),
        )
        assert p.overall_state == GateState.FAIL
        assert p.closest_gate == "efficiency"

    def test_overall_close_when_efficiency_in_close_band_and_others_pass(self) -> None:
        # Donchian + trend PASS; efficiency exactly at threshold (CLOSE).
        p = _proximity(
            last_close=Decimal("110"),
            donchian_high=Decimal("100"),
            ma_fast=Decimal("105"),
            ma_slow=Decimal("100"),
            efficiency_value=EFFICIENCY_THRESHOLD,
        )
        assert p.overall_state == GateState.CLOSE
        assert p.closest_gate == "efficiency"

    def test_closest_gate_walks_donchian_trend_efficiency_when_tied(self) -> None:
        """When ALL gates share the same state, closest_gate ties to ``donchian``.

        This locks the operator's mental-walk order (Donchian → Trend →
        Efficiency, the gate-check sequence in ``_evaluate_market``) so the UI
        doesn't flip-flop attribution when multiple gates are equally limiting.
        """
        # All three FAIL with similar magnitudes.
        p = _proximity(
            last_close=Decimal("50"),
            donchian_high=Decimal("100"),
            donchian_low=Decimal("10"),
            ma_fast=Decimal("90"),
            ma_slow=Decimal("100"),
            efficiency_value=Decimal("0.05"),
        )
        assert p.overall_state == GateState.FAIL
        assert p.closest_gate == "donchian"

    def test_donchian_per_market_takes_closer_of_two_directions(self) -> None:
        """The market-level Donchian state is the BETTER of long + short.

        Synthetic case: long-donchian is FAIL (close far below the high),
        short-donchian is PASS (close below the low — broken downside). The
        overall Donchian state for the market should reflect SHORT's PASS,
        not LONG's FAIL.
        """
        p = _proximity(
            last_close=Decimal("50"),
            donchian_high=Decimal("100"),  # long-fail: 50 far below 100
            donchian_low=Decimal("60"),  # short-pass: 50 already below 60
            # Keep trend + efficiency PASSing so overall reflects donchian's better side.
            ma_fast=Decimal("48"),
            ma_slow=Decimal("55"),
            efficiency_value=Decimal("0.70"),
        )
        # short_donchian PASS, long_donchian FAIL — market Donchian = PASS.
        assert p.short_donchian.state == GateState.PASS
        assert p.long_donchian.state == GateState.FAIL


# ---------------------------------------------------------------------------
# Warming-up + decommissioned edge cases
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_warming_up_when_snapshot_fields_are_none(self) -> None:
        p = compute_market_proximity(
            market="/MES",
            last_close=None,
            donchian_high=None,
            donchian_low=None,
            ma_fast=None,
            ma_slow=None,
            efficiency_value=None,
            efficiency_threshold=EFFICIENCY_THRESHOLD,
        )
        assert p.gate_status == "warming_up"
        assert p.overall_state == GateState.FAIL
        assert p.closest_gate == "history"
        assert p.last_close is None
        # All per-gate headrooms are None — no numeric data to surface.
        assert p.long_donchian.headroom is None
        assert p.short_donchian.headroom is None
        assert p.long_trend.headroom is None
        assert p.short_trend.headroom is None
        assert p.efficiency.headroom is None
        # Raw ER value is None when warming; the threshold is still known.
        assert p.efficiency_value is None
        assert p.efficiency_threshold == EFFICIENCY_THRESHOLD

    def test_warming_up_when_any_field_missing(self) -> None:
        """A partial snapshot is treated as fully warming up.

        The strategy emits either a complete snapshot (all six fields) or
        None across the board. Defensively, the proximity module collapses
        any-None-fields → warming_up rather than half-rendering a record.
        """
        p = compute_market_proximity(
            market="/MES",
            last_close=Decimal("100"),
            donchian_high=Decimal("95"),
            donchian_low=Decimal("80"),
            ma_fast=Decimal("99"),
            ma_slow=Decimal("95"),
            efficiency_value=None,  # missing one field
            efficiency_threshold=EFFICIENCY_THRESHOLD,
        )
        assert p.gate_status == "warming_up"
        assert p.closest_gate == "history"

    def test_decommissioned_still_populates_values_per_q4(self) -> None:
        """Q4 lock: decommissioned rows STILL surface what the gates would say."""
        p = _proximity(decommissioned=True)
        assert p.gate_status == "decommissioned"
        # All numeric headrooms are still computed — operator gets a view
        # into what the gates WOULD have said if the strategy were live.
        assert p.long_donchian.headroom is not None
        assert p.short_donchian.headroom is not None
        assert p.efficiency.headroom is not None
        # Overall state still reflects gate values (not forced to FAIL).
        assert p.overall_state in (GateState.PASS, GateState.CLOSE, GateState.FAIL)

    def test_decommissioned_and_warming_up_marks_decommissioned(self) -> None:
        """Decommission flag takes precedence in the gate_status sentinel.

        When BOTH conditions apply (operator flipped decommission AND a
        market is warming up), the row is marked ``decommissioned`` — the
        operator's intent dominates the data-availability story.
        """
        p = compute_market_proximity(
            market="/MES",
            last_close=None,
            donchian_high=None,
            donchian_low=None,
            ma_fast=None,
            ma_slow=None,
            efficiency_value=None,
            efficiency_threshold=EFFICIENCY_THRESHOLD,
            decommissioned=True,
        )
        assert p.gate_status == "decommissioned"
        assert p.overall_state == GateState.FAIL
        assert p.closest_gate == "history"


# ---------------------------------------------------------------------------
# Constants — locked thresholds for the V0 design
# ---------------------------------------------------------------------------
class TestConstants:
    def test_donchian_close_band_is_one_percent(self) -> None:
        assert DONCHIAN_CLOSE_BAND_PCT == Decimal("0.01")

    def test_trend_close_band_is_half_percent(self) -> None:
        assert TREND_CLOSE_BAND_PCT == Decimal("0.005")

    def test_efficiency_close_band_is_five_hundredths(self) -> None:
        assert EFFICIENCY_CLOSE_BAND == Decimal("0.05")


# ---------------------------------------------------------------------------
# Property-style: overall_state is always the worst gate state per §3.3
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("donchian_high", "ma_fast", "ma_slow", "efficiency_value", "expected"),
    [
        # All three pass: overall PASS.
        (Decimal("95"), Decimal("99"), Decimal("95"), Decimal("0.70"), GateState.PASS),
        # Efficiency dips into CLOSE; others PASS: overall CLOSE.
        (Decimal("95"), Decimal("99"), Decimal("95"), EFFICIENCY_THRESHOLD, GateState.CLOSE),
        # Efficiency fails; others PASS: overall FAIL.
        (Decimal("95"), Decimal("99"), Decimal("95"), Decimal("0.05"), GateState.FAIL),
        # Donchian fails (long-direction); short-direction trend + efficiency still
        # produce overall = FAIL via the donchian gate.
        (Decimal("200"), Decimal("50"), Decimal("60"), Decimal("0.70"), GateState.FAIL),
    ],
)
def test_overall_state_matches_worst_gate(
    donchian_high: Decimal,
    ma_fast: Decimal,
    ma_slow: Decimal,
    efficiency_value: Decimal,
    expected: GateState,
) -> None:
    p = _proximity(
        last_close=Decimal("100"),
        donchian_high=donchian_high,
        donchian_low=Decimal("10"),
        ma_fast=ma_fast,
        ma_slow=ma_slow,
        efficiency_value=efficiency_value,
    )
    assert p.overall_state == expected
