"""Unit tests for ``strategies/v1_trend_following/exit_proximity.py``.

Locks the per-trigger exit-state classification + headroom math + the
overall/closest-exit combiner from ``Docs/exit-proximity-design.md`` §3.2 +
§3.3, plus the strategy-side observation pass exposed on
``generate_exit_candidates`` (PR-A).

The proximity module is pure-Python and observation-only — the classification
tests exercise it directly with bare ``Decimal`` literals. The drift test at
the bottom is the load-bearing invariant: every exit the strategy actually
emits must show the matching trigger as TRIGGERED, so the display can never
lie about what closes.

Test naming follows claude-dev-guide §6.1: ``test_<unit>_<scenario>_<expected>``.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import pytest

from strategies.v1_trend_following import (
    Bar,
    BarSeries,
    CandidateSignal,
    Direction,
    Position,
    V1TrendFollowing,
    default_v1_parameters,
)
from strategies.v1_trend_following.exit_proximity import (
    EXIT_STOP_NEAR_BAND_PCT,
    EXIT_TREND_NEAR_BAND_PCT,
    ExitState,
    ExitTriggerProximity,
    classify_reversal,
    classify_stop,
    combine_overall_exit,
    compute_position_exit_proximity,
    directional_reversal_entry_state,
)
from strategies.v1_trend_following.proximity import GateState, compute_market_proximity

MIN_HOLDING = 14  # V1 default (locked)


def _exit(**overrides: object):
    """Baseline ``PositionExitProximity``: LONG, held 20d (MIN_HOLDING met),
    close 100 well above ma_fast 90 (trend_flip HOLDING), no stop, no reversal,
    not decommissioned. Overrides toggle one input per test."""
    base: dict[str, object] = {
        "market": "/MES",
        "direction": "long",
        "held_days": 20,
        "last_close": Decimal("100"),
        "ma_fast": Decimal("90"),
        "min_holding_days": MIN_HOLDING,
        "decommissioned": False,
        "stop_price": None,
        "reversal_entry_state": None,
    }
    base.update(overrides)
    return compute_position_exit_proximity(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (c) trend_flip — deterioration gate (MIN_HOLDING already satisfied)
# ---------------------------------------------------------------------------
class TestTrendFlipDeterioration:
    def test_long_close_well_above_ma_fast_is_holding(self) -> None:
        p = _exit(last_close=Decimal("100"), ma_fast=Decimal("90"))
        assert p.trend_flip.state is ExitState.HOLDING

    def test_long_close_within_band_above_ma_fast_is_near(self) -> None:
        # (100 - 99.6)/100 = 0.004 ≤ 0.005 band → NEAR.
        p = _exit(last_close=Decimal("100"), ma_fast=Decimal("99.6"))
        assert p.trend_flip.state is ExitState.NEAR
        assert p.trend_flip.headroom is not None
        assert p.trend_flip.headroom <= EXIT_TREND_NEAR_BAND_PCT

    def test_long_close_below_ma_fast_is_triggered(self) -> None:
        # close < ma_fast → deterioration < 0 → flip condition met.
        p = _exit(last_close=Decimal("99"), ma_fast=Decimal("100"))
        assert p.trend_flip.state is ExitState.TRIGGERED
        assert p.trend_flip.headroom is not None
        assert p.trend_flip.headroom < 0

    def test_long_close_exactly_on_ma_fast_is_triggered(self) -> None:
        # deterioration == 0 resolves to TRIGGERED (the flip predicate is
        # close < ma_fast strictly, but proximity treats touching the line as
        # closing — boundary <= 0 → TRIGGERED).
        p = _exit(last_close=Decimal("100"), ma_fast=Decimal("100"))
        assert p.trend_flip.state is ExitState.TRIGGERED

    def test_short_mirror_close_below_ma_fast_is_holding(self) -> None:
        p = _exit(direction="short", last_close=Decimal("100"), ma_fast=Decimal("110"))
        assert p.trend_flip.state is ExitState.HOLDING

    def test_short_close_within_band_below_ma_fast_is_near(self) -> None:
        # short deterioration = (ma_fast - close)/close = (100.4-100)/100 = 0.004.
        p = _exit(direction="short", last_close=Decimal("100"), ma_fast=Decimal("100.4"))
        assert p.trend_flip.state is ExitState.NEAR

    def test_short_close_above_ma_fast_is_triggered(self) -> None:
        p = _exit(direction="short", last_close=Decimal("101"), ma_fast=Decimal("100"))
        assert p.trend_flip.state is ExitState.TRIGGERED


# ---------------------------------------------------------------------------
# (c) trend_flip — MIN_HOLDING-days gate
# ---------------------------------------------------------------------------
class TestHoldingDaysGate:
    def test_held_well_past_floor_defers_to_deterioration(self) -> None:
        # held 20 > 14 → gate satisfied → deterioration governs (HOLDING here).
        p = _exit(held_days=20, last_close=Decimal("100"), ma_fast=Decimal("90"))
        assert p.gate_status == "active"
        assert p.trend_flip.state is ExitState.HOLDING

    def test_held_exactly_at_floor_defers_to_deterioration(self) -> None:
        # held == MIN_HOLDING → headroom 0, NOT > 0 → gate satisfied.
        p = _exit(held_days=MIN_HOLDING, last_close=Decimal("99"), ma_fast=Decimal("100"))
        assert p.gate_status == "active"
        assert p.trend_flip.state is ExitState.TRIGGERED

    def test_within_3_days_of_floor_is_min_holding_blocked_near(self) -> None:
        # held 12 → headroom 2 (0 < 2 ≤ 3) → NEAR, capped on the holding-days
        # gate EVEN THOUGH close has crossed below ma_fast.
        p = _exit(held_days=12, last_close=Decimal("90"), ma_fast=Decimal("100"))
        assert p.gate_status == "min_holding_blocked"
        assert p.trend_flip.state is ExitState.NEAR
        assert p.trend_flip.detail is not None
        assert "2d left" in p.trend_flip.detail

    def test_far_from_floor_is_min_holding_blocked_holding(self) -> None:
        # held 5 → headroom 9 (> 3) → HOLDING, capped (flip can't fire yet).
        p = _exit(held_days=5, last_close=Decimal("90"), ma_fast=Decimal("100"))
        assert p.gate_status == "min_holding_blocked"
        assert p.trend_flip.state is ExitState.HOLDING

    def test_opened_at_unknown_skips_floor(self) -> None:
        # held_days None → strategy skips the floor → deterioration governs.
        p = _exit(held_days=None, last_close=Decimal("99"), ma_fast=Decimal("100"))
        assert p.gate_status == "active"
        assert p.trend_flip.state is ExitState.TRIGGERED
        assert p.trend_flip.detail == "holding-days unknown"


# ---------------------------------------------------------------------------
# (a) stop distance
# ---------------------------------------------------------------------------
class TestStopDistance:
    def test_long_far_above_stop_is_holding(self) -> None:
        s = classify_stop(last_close=Decimal("100"), stop_price=Decimal("90"), direction="long")
        assert s.state is ExitState.HOLDING

    def test_long_within_1pct_of_stop_is_near(self) -> None:
        # (100 - 99.5)/100 = 0.005 ≤ 0.01 → NEAR.
        s = classify_stop(last_close=Decimal("100"), stop_price=Decimal("99.5"), direction="long")
        assert s.state is ExitState.NEAR
        assert s.headroom is not None
        assert s.headroom <= EXIT_STOP_NEAR_BAND_PCT

    def test_long_at_or_below_stop_is_triggered(self) -> None:
        assert (
            classify_stop(
                last_close=Decimal("100"), stop_price=Decimal("100"), direction="long"
            ).state
            is ExitState.TRIGGERED
        )
        assert (
            classify_stop(
                last_close=Decimal("100"), stop_price=Decimal("101"), direction="long"
            ).state
            is ExitState.TRIGGERED
        )

    def test_short_mirror(self) -> None:
        assert (
            classify_stop(
                last_close=Decimal("100"), stop_price=Decimal("110"), direction="short"
            ).state
            is ExitState.HOLDING
        )
        assert (
            classify_stop(
                last_close=Decimal("100"), stop_price=Decimal("100.5"), direction="short"
            ).state
            is ExitState.NEAR
        )
        assert (
            classify_stop(
                last_close=Decimal("100"), stop_price=Decimal("100"), direction="short"
            ).state
            is ExitState.TRIGGERED
        )

    def test_no_stop_price_is_null_state_with_detail(self) -> None:
        s = classify_stop(last_close=Decimal("100"), stop_price=None, direction="long")
        assert s.state is ExitState.HOLDING
        assert s.headroom is None
        assert s.detail is not None and "no stop" in s.detail.lower()

    def test_no_mark_is_null_state(self) -> None:
        s = classify_stop(last_close=None, stop_price=Decimal("99"), direction="long")
        assert s.headroom is None


# ---------------------------------------------------------------------------
# (b) reversal — derived mapping
# ---------------------------------------------------------------------------
class TestReversalMapping:
    def test_entry_pass_maps_to_triggered(self) -> None:
        assert classify_reversal(GateState.PASS).state is ExitState.TRIGGERED

    def test_entry_close_maps_to_near(self) -> None:
        assert classify_reversal(GateState.CLOSE).state is ExitState.NEAR

    def test_entry_fail_maps_to_holding(self) -> None:
        assert classify_reversal(GateState.FAIL).state is ExitState.HOLDING

    def test_none_maps_to_holding_with_detail(self) -> None:
        r = classify_reversal(None)
        assert r.state is ExitState.HOLDING
        assert r.detail is not None

    def test_directional_state_for_held_long_reads_short_gates(self) -> None:
        # Held LONG → reversal is a SHORT entry. Build a market where the SHORT
        # gates all PASS: close 80 < donchian_low 85 (short donchian broken),
        # 80 < ma_fast 90 < ma_slow 100 (short trend), efficiency 0.60 ≥ 0.25.
        mp = compute_market_proximity(
            market="/MES",
            last_close=Decimal("80"),
            donchian_high=Decimal("120"),
            donchian_low=Decimal("85"),
            ma_fast=Decimal("90"),
            ma_slow=Decimal("100"),
            efficiency_value=Decimal("0.60"),
            efficiency_threshold=Decimal("0.20"),
        )
        state = directional_reversal_entry_state(market_proximity=mp, held_direction="long")
        assert state is GateState.PASS

    def test_directional_state_none_market_is_none(self) -> None:
        assert (
            directional_reversal_entry_state(market_proximity=None, held_direction="long") is None
        )


# ---------------------------------------------------------------------------
# (d) decommission
# ---------------------------------------------------------------------------
class TestDecommission:
    def test_armed_is_triggered_and_takes_precedence(self) -> None:
        # Everything else HOLDING, but decommission armed → overall TRIGGERED,
        # closest_exit names decommission.
        p = _exit(decommissioned=True, last_close=Decimal("100"), ma_fast=Decimal("90"))
        assert p.decommission.state is ExitState.TRIGGERED
        assert p.overall_state is ExitState.TRIGGERED
        assert p.closest_exit == "decommission"
        assert p.gate_status == "decommissioned"

    def test_not_armed_is_holding(self) -> None:
        p = _exit(decommissioned=False)
        assert p.decommission.state is ExitState.HOLDING


# ---------------------------------------------------------------------------
# Warming up / missing data
# ---------------------------------------------------------------------------
class TestWarmingUp:
    def test_no_snapshot_is_warming_up_with_null_trend_flip(self) -> None:
        p = _exit(last_close=None, ma_fast=None)
        assert p.gate_status == "warming_up"
        assert p.trend_flip.state is ExitState.HOLDING
        assert p.trend_flip.headroom is None
        assert p.overall_state is ExitState.HOLDING

    def test_warming_up_closest_exit_is_not_decommission(self) -> None:
        # Regression: an all-HOLDING / all-None-headroom row must not default
        # its closest_exit label to the (un-armed) kill switch.
        p = _exit(last_close=None, ma_fast=None)
        assert p.closest_exit != "decommission"


# ---------------------------------------------------------------------------
# Overall state + closest_exit (§3.3) + the "overall == worst" property
# ---------------------------------------------------------------------------
class TestOverallAndClosest:
    def test_stop_near_drives_overall_when_others_holding(self) -> None:
        p = _exit(
            last_close=Decimal("100"),
            ma_fast=Decimal("90"),  # trend_flip HOLDING
            stop_price=Decimal("99.5"),  # stop NEAR
        )
        assert p.overall_state is ExitState.NEAR
        assert p.closest_exit == "stop"

    def test_closest_exit_breaks_tie_by_smallest_headroom(self) -> None:
        # trend_flip NEAR (det 0.004) vs stop NEAR (0.005) → trend_flip smaller.
        p = _exit(
            last_close=Decimal("100"),
            ma_fast=Decimal("99.6"),  # trend_flip NEAR, headroom 0.004
            stop_price=Decimal("99.5"),  # stop NEAR, headroom 0.005
        )
        assert p.overall_state is ExitState.NEAR
        assert p.closest_exit == "trend_flip"

    @pytest.mark.parametrize(
        ("kwargs"),
        [
            {"last_close": Decimal("100"), "ma_fast": Decimal("90")},
            {"last_close": Decimal("99"), "ma_fast": Decimal("100")},
            {"last_close": Decimal("100"), "ma_fast": Decimal("90"), "stop_price": Decimal("99.5")},
            {
                "last_close": Decimal("100"),
                "ma_fast": Decimal("90"),
                "reversal_entry_state": GateState.PASS,
            },
            {"decommissioned": True, "last_close": Decimal("100"), "ma_fast": Decimal("90")},
            {"last_close": None, "ma_fast": None},
        ],
    )
    def test_property_overall_equals_worst_trigger(self, kwargs: dict[str, object]) -> None:
        rank = {ExitState.TRIGGERED: 2, ExitState.NEAR: 1, ExitState.HOLDING: 0}
        p = _exit(**kwargs)
        worst = max(
            (p.trend_flip.state, p.stop.state, p.reversal.state, p.decommission.state),
            key=lambda s: rank[s],
        )
        assert rank[p.overall_state] == rank[worst]


# ---------------------------------------------------------------------------
# combine_overall_exit — the §3.3 combiner, shared by PR-A compute + PR-B
# api-side stop enrichment. The "flip" cases are the load-bearing PR-B
# invariant: re-deriving with the joined stop must move overall_state /
# closest_exit away from LEAN's stop-blind (NULL-state stop) values.
# ---------------------------------------------------------------------------
class TestCombineOverallExit:
    @staticmethod
    def _holding() -> ExitTriggerProximity:
        return ExitTriggerProximity(state=ExitState.HOLDING, headroom=None)

    def test_decommission_triggered_wins_outright(self) -> None:
        # Even with a more-breached stop, the kill switch takes precedence.
        overall, closest = combine_overall_exit(
            trend_flip=ExitTriggerProximity(state=ExitState.TRIGGERED, headroom=Decimal("-0.02")),
            stop=ExitTriggerProximity(state=ExitState.TRIGGERED, headroom=Decimal("-0.05")),
            reversal=self._holding(),
            decommission=ExitTriggerProximity(state=ExitState.TRIGGERED, headroom=None),
        )
        assert overall is ExitState.TRIGGERED
        assert closest == "decommission"

    def test_joined_stop_triggered_flips_overall_from_holding(self) -> None:
        # PR-B enrichment flip: LEAN emitted a NULL-state (HOLDING) stop and an
        # all-HOLDING summary; the api joins a BREACHED stop (long mark below
        # the stop) via classify_stop and re-derives → TRIGGERED / closest=stop.
        stop = classify_stop(last_close=Decimal("100"), stop_price=Decimal("101"), direction="long")
        assert stop.state is ExitState.TRIGGERED  # sanity: breach detected
        overall, closest = combine_overall_exit(
            trend_flip=self._holding(),
            stop=stop,
            reversal=self._holding(),
            decommission=self._holding(),
        )
        assert overall is ExitState.TRIGGERED
        assert closest == "stop"

    def test_joined_stop_near_flips_overall_from_holding(self) -> None:
        # A joined stop within the 1% band flips an otherwise-HOLDING row to NEAR.
        stop = classify_stop(
            last_close=Decimal("100"), stop_price=Decimal("99.5"), direction="long"
        )
        assert stop.state is ExitState.NEAR
        overall, closest = combine_overall_exit(
            trend_flip=self._holding(),
            stop=stop,
            reversal=self._holding(),
            decommission=self._holding(),
        )
        assert overall is ExitState.NEAR
        assert closest == "stop"

    def test_no_joined_stop_stays_holding(self) -> None:
        # No working stop on record → classify_stop NULL-state HOLDING; with all
        # other triggers HOLDING the row stays HOLDING (closest is an indicator).
        stop = classify_stop(last_close=Decimal("100"), stop_price=None, direction="long")
        assert stop.state is ExitState.HOLDING
        overall, closest = combine_overall_exit(
            trend_flip=self._holding(),
            stop=stop,
            reversal=self._holding(),
            decommission=self._holding(),
        )
        assert overall is ExitState.HOLDING
        assert closest != "decommission"

    def test_tie_break_by_smallest_headroom(self) -> None:
        # trend_flip NEAR (0.004) vs stop NEAR (0.005) → trend_flip is closer.
        overall, closest = combine_overall_exit(
            trend_flip=ExitTriggerProximity(state=ExitState.NEAR, headroom=Decimal("0.004")),
            stop=ExitTriggerProximity(state=ExitState.NEAR, headroom=Decimal("0.005")),
            reversal=self._holding(),
            decommission=self._holding(),
        )
        assert overall is ExitState.NEAR
        assert closest == "trend_flip"


# ---------------------------------------------------------------------------
# Drift test — every emitted exit ⇒ matching trigger TRIGGERED
# ---------------------------------------------------------------------------
def _make_bar(d: date, *, close: Decimal) -> Bar:
    return Bar(
        session_date=d,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=10_000,
    )


def _trending_series(
    market: str, *, n_bars: int = 250, last_close: Decimal | None = None
) -> BarSeries:
    start = date(2026, 1, 1)
    bars: list[Bar] = []
    for i in range(n_bars):
        close = (Decimal("100") * (Decimal("1.002") ** i)).quantize(Decimal("0.01"))
        bars.append(_make_bar(start + timedelta(days=i), close=close))
    if last_close is not None:
        bars[-1] = _make_bar(bars[-1].session_date, close=last_close)
    return BarSeries(market=market, bars=tuple(bars))


def _long_position(market: str, *, opened_at: date | None) -> Position:
    return Position(
        market=market,
        direction=Direction.LONG,
        quantity=2,
        avg_cost=Decimal("110"),
        opened_at_session_date=opened_at,
    )


class TestDriftVsGenerateExitCandidates:
    """The load-bearing invariant (§11): the displayed proximity cannot drift
    from what ``generate_exit_candidates`` actually closes."""

    def test_one_row_per_held_non_flat_position(self) -> None:
        strategy = V1TrendFollowing(default_v1_parameters())
        series = _trending_series("/MES")
        as_of = series.bars[-1].session_date
        positions = {
            "/MES": _long_position("/MES", opened_at=as_of - timedelta(days=30)),
            "/M2K": Position(
                market="/M2K",
                direction=Direction.FLAT,
                quantity=0,
                avg_cost=Decimal("0"),
                opened_at_session_date=None,
            ),
        }
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions=positions,
            as_of_session_date=as_of,
        )
        markets = {e.market for e in result.position_exit_evaluations}
        assert markets == {"/MES"}  # FLAT excluded

    def test_trend_flip_emission_shows_trend_flip_triggered(self) -> None:
        strategy = V1TrendFollowing(default_v1_parameters())
        # last_close 60 << MA_FAST (natural trend ~160) → LONG trend flip; held
        # 30d > MIN_HOLDING so the floor doesn't block it.
        series = _trending_series("/MES", last_close=Decimal("60"))
        as_of = series.bars[-1].session_date
        positions = {"/MES": _long_position("/MES", opened_at=as_of - timedelta(days=30))}
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions=positions,
            as_of_session_date=as_of,
        )
        emitted = {s.market: s for s in result.signals}
        assert emitted["/MES"].exit_reason == "trend_flip"
        prox = {e.market: e for e in result.position_exit_evaluations}["/MES"]
        assert prox.trend_flip.state is ExitState.TRIGGERED
        assert prox.overall_state is ExitState.TRIGGERED

    def test_decommission_emission_shows_decommission_triggered(self) -> None:
        params = dataclasses.replace(default_v1_parameters(), strategy_decommissioned=True)
        strategy = V1TrendFollowing(params)
        series = _trending_series("/MES")
        as_of = series.bars[-1].session_date
        positions = {"/MES": _long_position("/MES", opened_at=as_of - timedelta(days=30))}
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions=positions,
            as_of_session_date=as_of,
        )
        emitted = {s.market: s for s in result.signals}
        assert emitted["/MES"].exit_reason == "decommission"
        prox = {e.market: e for e in result.position_exit_evaluations}["/MES"]
        assert prox.decommission.state is ExitState.TRIGGERED
        assert prox.gate_status == "decommissioned"

    def test_reversal_emission_shows_reversal_triggered(self) -> None:
        strategy = V1TrendFollowing(default_v1_parameters())
        series = _trending_series("/MES")
        as_of = series.bars[-1].session_date
        positions = {"/MES": _long_position("/MES", opened_at=as_of - timedelta(days=30))}
        # Inject an opposite-direction (SHORT) entry candidate → reversal fires.
        short_candidate = CandidateSignal(
            market="/MES",
            direction=Direction.SHORT,
            signal_type="donchian_breakout",
            session_date=as_of,
            decision_price=Decimal("60.00"),
            stop_price=Decimal("63.00"),
            indicators_snapshot={"atr": Decimal("1.00")},
        )
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions=positions,
            as_of_session_date=as_of,
            entry_candidates=(short_candidate,),
        )
        emitted = {s.market: s for s in result.signals}
        assert emitted["/MES"].exit_reason == "reversal"
        prox = {e.market: e for e in result.position_exit_evaluations}["/MES"]
        assert prox.reversal.state is ExitState.TRIGGERED

    def test_reversal_proximity_derives_near_from_entry_evaluations(self) -> None:
        # No opposite candidate fired, but the SHORT entry gates are CLOSE →
        # reversal proximity should read NEAR (derived from entry_evaluations).
        strategy = V1TrendFollowing(default_v1_parameters())
        series = _trending_series("/MES")
        as_of = series.bars[-1].session_date
        positions = {"/MES": _long_position("/MES", opened_at=as_of - timedelta(days=30))}
        # Craft a MarketProximity whose SHORT side is CLOSE: close just above
        # donchian_low (short donchian CLOSE) and short trend CLOSE, efficiency PASS.
        entry_eval = compute_market_proximity(
            market="/MES",
            last_close=Decimal("100"),
            donchian_high=Decimal("130"),
            donchian_low=Decimal("99.5"),  # (100-99.5)/100 = 0.005 → short donchian CLOSE
            ma_fast=Decimal("100.2"),
            ma_slow=Decimal("100.6"),  # short trend gaps within band → CLOSE
            efficiency_value=Decimal("0.60"),
            efficiency_threshold=Decimal("0.20"),
        )
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions=positions,
            as_of_session_date=as_of,
            entry_evaluations=(entry_eval,),
        )
        prox = {e.market: e for e in result.position_exit_evaluations}["/MES"]
        assert prox.reversal.state is ExitState.NEAR
