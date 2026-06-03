"""Unit tests for V1TrendFollowing.generate_exit_candidates.

Covers each branch enumerated in ``Docs/exit-pipeline-design.md §10.1``:
the three exit conditions (b reversal / c trend_flip / d decommission), their
precedence ordering, the rejection reasons (TREND_HOLDS,
MIN_HOLDING_NOT_REACHED, INSUFFICIENT_BAR_HISTORY), the CandidateSignal
fields specific to exits (sentinel direction/stop, prior_position_*,
paired_entry_market), and the entry-side STRATEGY_DECOMMISSIONED
short-circuit added to ``_evaluate_market``.

Test naming follows claude-dev-guide §6.1: ``test_<unit>_<scenario>_<expected>``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from strategies.v1_trend_following import (
    Bar,
    BarSeries,
    CandidateSignal,
    Direction,
    ExitGenerationResult,
    Position,
    RejectionReason,
    V1Parameters,
    V1TrendFollowing,
    default_v1_parameters,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_bar(d: date, *, close: Decimal, ohl_offset: Decimal = Decimal("1.00")) -> Bar:
    return Bar(
        session_date=d,
        open=close - ohl_offset / 2,
        high=close + ohl_offset,
        low=close - ohl_offset,
        close=close,
        volume=10_000,
    )


def _trending_series(
    market: str,
    *,
    n_bars: int = 250,
    base: Decimal = Decimal("100"),
    last_close: Decimal | None = None,
) -> BarSeries:
    """Upward-trending series (0.2%/day drift) suitable for the exit tests.

    Without ``last_close`` the series ends at the natural trend value (well
    above MA_FAST). With ``last_close`` the final bar is replaced — set it
    BELOW MA_FAST (e.g. ``Decimal('60')``) to trigger a LONG trend flip, or
    well above MA_FAST (e.g. ``Decimal('160')``) for the no-flip case.
    """
    start = date(2026, 1, 1)
    bars: list[Bar] = []
    for i in range(n_bars):
        drift = base * (Decimal("1.002") ** i)
        wave = Decimal(str(0.5 + 0.5 * ((i % 7) / 6)))
        close = (drift + wave).quantize(Decimal("0.01"))
        bars.append(_make_bar(start + timedelta(days=i), close=close))
    if last_close is not None:
        last_date = bars[-1].session_date
        bars[-1] = _make_bar(last_date, close=last_close)
    return BarSeries(market=market, bars=tuple(bars))


def _long_position(
    market: str,
    *,
    opened_at: date | None,
    quantity: int = 2,
    avg_cost: Decimal = Decimal("110"),
) -> Position:
    return Position(
        market=market,
        direction=Direction.LONG,
        quantity=quantity,
        avg_cost=avg_cost,
        opened_at_session_date=opened_at,
    )


def _short_position(
    market: str,
    *,
    opened_at: date | None,
    quantity: int = -1,
    avg_cost: Decimal = Decimal("110"),
) -> Position:
    return Position(
        market=market,
        direction=Direction.SHORT,
        quantity=quantity,
        avg_cost=avg_cost,
        opened_at_session_date=opened_at,
    )


def _flat_position(market: str) -> Position:
    return Position(
        market=market,
        direction=Direction.FLAT,
        quantity=0,
        avg_cost=Decimal("0"),
        opened_at_session_date=None,
    )


def _short_entry_candidate(market: str, session_date: date) -> CandidateSignal:
    """Synthetic SHORT entry candidate; mirrors what generate_signals would
    emit on a downward Donchian breakout that passes trend + Efficiency Ratio.

    Tests use this to exercise reversal coupling without re-running the entry
    pipeline against a contrived price series."""
    return CandidateSignal(
        market=market,
        direction=Direction.SHORT,
        signal_type="donchian_breakout",
        session_date=session_date,
        decision_price=Decimal("60.00"),
        stop_price=Decimal("63.00"),
        indicators_snapshot={
            "donchian_high": Decimal("100"),
            "donchian_low": Decimal("70"),
            "ma_fast": Decimal("90"),
            "ma_slow": Decimal("95"),
            "efficiency_ratio": Decimal("0.65"),
            "atr": Decimal("1.00"),
        },
    )


def _strategy_with_decommission(*, decommissioned: bool) -> V1TrendFollowing:
    params = dataclasses.replace(default_v1_parameters(), strategy_decommissioned=decommissioned)
    return V1TrendFollowing(params)


@pytest.fixture(scope="module")
def strategy() -> V1TrendFollowing:
    return V1TrendFollowing(default_v1_parameters())


# ---------------------------------------------------------------------------
# Per-branch tests — design doc §10.1
# ---------------------------------------------------------------------------
class TestGenerateExitCandidates:
    def test_no_positions_no_signals(self, strategy: V1TrendFollowing) -> None:
        """Empty current_positions → empty result. The exit pipeline owns
        held positions only; flat / unknown markets are out of scope."""
        result = strategy.generate_exit_candidates(
            active_universe={},
            current_positions={},
            as_of_session_date=date(2026, 5, 1),
        )
        assert isinstance(result, ExitGenerationResult)
        assert result.signals == ()
        assert result.rejections == ()

    def test_flat_position_skipped(self, strategy: V1TrendFollowing) -> None:
        """A FLAT-direction Position record is treated as no position — the
        exit pipeline emits no signal AND records no rejection for it
        (rejections are about *deferring* an exit on a held position)."""
        series = _trending_series("/MES")
        last_date = series.bars[-1].session_date
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": _flat_position("/MES")},
            as_of_session_date=last_date,
        )
        assert result.signals == ()
        assert result.rejections == ()

    def test_long_position_no_trend_flip_emits_trend_holds(
        self, strategy: V1TrendFollowing
    ) -> None:
        """LONG held + last_close > MA_FAST → TREND_HOLDS rejection.

        The upward-drifting series ends with close ≈ 160 (well above MA_FAST
        which sits near the trend mid-point), so the trend has NOT flipped
        against the LONG. No reversal candidate is provided; no decommission
        — TREND_HOLDS is the only valid outcome."""
        series = _trending_series("/MES", last_close=Decimal("160"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        assert result.signals == ()
        assert result.rejections == (("/MES", RejectionReason.TREND_HOLDS),)

    def test_long_position_trend_flip_emits_exit(self, strategy: V1TrendFollowing) -> None:
        """LONG held + last_close < MA_FAST + held ≥ MIN_HOLDING → 1 exit
        signal with exit_reason='trend_flip'."""
        series = _trending_series("/MES", last_close=Decimal("60"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        assert result.rejections == ()
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.market == "/MES"
        assert sig.signal_type == "exit"
        assert sig.exit_reason == "trend_flip"
        assert sig.direction is Direction.FLAT
        assert sig.paired_entry_market is None

    def test_long_position_trend_flip_blocked_by_min_holding(
        self, strategy: V1TrendFollowing
    ) -> None:
        """LONG held + close < MA_FAST + held < MIN_HOLDING_DAYS=14 →
        MIN_HOLDING_NOT_REACHED rejection.

        The trend has actually flipped; the gate is purely the holding
        floor. Per design L2 and backend-spec §2.3 the floor guards against
        whipsaw exits in the first few days of a position."""
        series = _trending_series("/MES", last_close=Decimal("60"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=3))
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        assert result.signals == ()
        assert result.rejections == (("/MES", RejectionReason.MIN_HOLDING_NOT_REACHED),)

    def test_short_position_trend_flip_emits_exit(self, strategy: V1TrendFollowing) -> None:
        """SHORT held + last_close > MA_FAST + held ≥ MIN_HOLDING → 1 exit
        signal. The mirror of the LONG case."""
        series = _trending_series("/MES")  # natural ending close ≈ 165
        last_date = series.bars[-1].session_date
        position = _short_position("/MES", opened_at=last_date - timedelta(days=30))
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.exit_reason == "trend_flip"
        assert sig.prior_position_direction is Direction.SHORT

    def test_reversal_with_paired_entry_emits_reversal(self, strategy: V1TrendFollowing) -> None:
        """LONG held + entry pipeline produced a SHORT candidate for the
        same market → reversal exit with paired_entry_market set. The
        sequencing (exit first, paired entry second) is the dispatcher's
        responsibility in PR-C; the strategy only emits the linkage."""
        series = _trending_series("/MES")
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        entry_candidate = _short_entry_candidate("/MES", last_date)

        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
            entry_candidates=(entry_candidate,),
        )
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.exit_reason == "reversal"
        assert sig.paired_entry_market == "/MES"
        assert sig.prior_position_direction is Direction.LONG
        assert sig.prior_position_quantity == position.quantity

    def test_reversal_without_paired_entry_does_not_emit_reversal(
        self, strategy: V1TrendFollowing
    ) -> None:
        """LONG held; the entry pipeline rejected the opposite SHORT
        (e.g. EFFICIENCY_BELOW_THRESHOLD), so entry_candidates is empty for
        this market. The exit pipeline must NOT synthesize a reversal —
        per design §3 "Donchian-alone is insufficient" — and must fall
        through to the trend_flip evaluation."""
        series = _trending_series("/MES", last_close=Decimal("160"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
            entry_candidates=(),  # opposite breakout did not pass entry filters
        )
        # No reversal: assert no exit_reason=='reversal' in any emission,
        # and that we got the TREND_HOLDS rejection from the fall-through.
        assert all(s.exit_reason != "reversal" for s in result.signals)
        assert result.rejections == (("/MES", RejectionReason.TREND_HOLDS),)

    def test_reversal_ignored_when_no_position_held(self, strategy: V1TrendFollowing) -> None:
        """An entry candidate without a corresponding held position is
        invisible to the exit pipeline — there is nothing to reverse out
        of. The exit pipeline iterates over held positions only."""
        series = _trending_series("/MES")
        last_date = series.bars[-1].session_date
        entry_candidate = _short_entry_candidate("/MES", last_date)
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={},
            as_of_session_date=last_date,
            entry_candidates=(entry_candidate,),
        )
        assert result.signals == ()
        assert result.rejections == ()

    def test_reversal_ignored_for_same_direction_entry(self, strategy: V1TrendFollowing) -> None:
        """A LONG-position + LONG entry candidate is NOT a reversal — that's
        the same-direction case handled (and rejected) by the entry pipeline
        (POSITION_ALREADY_SAME_DIRECTION). The exit pipeline must not treat
        it as a reversal trigger; the held LONG falls through to trend_flip
        evaluation."""
        series = _trending_series("/MES", last_close=Decimal("160"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        long_entry = CandidateSignal(
            market="/MES",
            direction=Direction.LONG,
            signal_type="donchian_breakout",
            session_date=last_date,
            decision_price=Decimal("160"),
            stop_price=Decimal("150"),
            indicators_snapshot={},
        )
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
            entry_candidates=(long_entry,),
        )
        assert all(s.exit_reason != "reversal" for s in result.signals)

    def test_decommission_emits_for_held_position(self) -> None:
        """strategy_decommissioned=True + held LONG → 1 exit signal,
        exit_reason='decommission'. Indicator state is irrelevant — the
        decommission overrides everything (per L6)."""
        strat = _strategy_with_decommission(decommissioned=True)
        series = _trending_series("/MES", last_close=Decimal("160"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        result = strat.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        assert len(result.signals) == 1
        assert result.signals[0].exit_reason == "decommission"

    def test_decommission_no_position_emits_nothing(self) -> None:
        """strategy_decommissioned=True + no held position → no signal.
        Decommission emits CLOSEs only for markets with held positions;
        there's nothing to close if we're flat."""
        strat = _strategy_with_decommission(decommissioned=True)
        result = strat.generate_exit_candidates(
            active_universe={},
            current_positions={},
            as_of_session_date=date(2026, 5, 1),
        )
        assert result.signals == ()
        assert result.rejections == ()

    def test_decommission_precedence_over_reversal(self) -> None:
        """strategy_decommissioned=True + a valid reversal entry candidate
        → decommission wins (per design §3 precedence + L6 operator-intent:
        decommission winds everything down; reversal would open a new
        opposite position which contradicts that intent)."""
        strat = _strategy_with_decommission(decommissioned=True)
        series = _trending_series("/MES")
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        entry_candidate = _short_entry_candidate("/MES", last_date)
        result = strat.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
            entry_candidates=(entry_candidate,),
        )
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.exit_reason == "decommission"
        # Decommission overrides — no paired_entry_market on a
        # decommission candidate even though the entry candidate exists.
        assert sig.paired_entry_market is None

    def test_reversal_precedence_over_trend_flip(self, strategy: V1TrendFollowing) -> None:
        """LONG held + close < MA_FAST (trend would flip) + entry candidate
        SHORT (reversal would fire) → reversal wins (it triggers a paired
        follow-on entry; losing it to trend_flip would emit only the close
        and leave the cycle without the new direction)."""
        series = _trending_series("/MES", last_close=Decimal("60"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        entry_candidate = _short_entry_candidate("/MES", last_date)
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
            entry_candidates=(entry_candidate,),
        )
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.exit_reason == "reversal"
        assert sig.paired_entry_market == "/MES"

    def test_insufficient_bars_emits_history_rejection(self, strategy: V1TrendFollowing) -> None:
        """LONG held + series shorter than min_required_bars → no exit can
        be evaluated; emit INSUFFICIENT_BAR_HISTORY rejection. This mirrors
        the entry pipeline's behavior for the same condition."""
        short_series = BarSeries(
            market="/MES",
            bars=tuple(
                _make_bar(date(2026, 1, 1) + timedelta(days=i), close=Decimal("100"))
                for i in range(50)  # < min_required_bars=200
            ),
        )
        last_date = short_series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": short_series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        assert result.signals == ()
        assert result.rejections == (("/MES", RejectionReason.INSUFFICIENT_BAR_HISTORY),)

    def test_missing_opened_at_skips_min_holding_check(self, strategy: V1TrendFollowing) -> None:
        """LONG held + opened_at_session_date=None → MIN_HOLDING check is
        conservatively skipped (matches the entry-side _evaluate_market
        behavior). Trend-flip evaluation still runs; with a flipped series
        the exit fires."""
        series = _trending_series("/MES", last_close=Decimal("60"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=None)
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        assert result.signals != ()
        assert result.signals[0].exit_reason == "trend_flip"
        # No MIN_HOLDING_NOT_REACHED rejection — the check was bypassed.
        rejection_reasons = {r for _, r in result.rejections}
        assert RejectionReason.MIN_HOLDING_NOT_REACHED not in rejection_reasons

    def test_exit_candidate_has_flat_direction_and_zero_stop(
        self, strategy: V1TrendFollowing
    ) -> None:
        """Sentinel fields on an exit signal:

        - ``direction == Direction.FLAT`` (target ending position is flat;
          dispatcher computes actual buy/sell side at place-order time).
        - ``stop_price == Decimal('0')`` (no new bracket stop on a close).
        - ``decision_price`` reflects the last bar's close.
        """
        series = _trending_series("/MES", last_close=Decimal("60"))
        last_date = series.bars[-1].session_date
        position = _long_position("/MES", opened_at=last_date - timedelta(days=30))
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        sig = result.signals[0]
        assert sig.direction is Direction.FLAT
        assert sig.stop_price == Decimal("0")
        assert sig.decision_price == series.bars[-1].close

    def test_exit_candidate_includes_prior_position_state(self, strategy: V1TrendFollowing) -> None:
        """prior_position_direction / prior_position_quantity capture the
        position at signal-emit time, for audit-trail comparison against
        the dispatcher's fresh read at place-order time (design §Q3)."""
        series = _trending_series("/MES", last_close=Decimal("60"))
        last_date = series.bars[-1].session_date
        position = _long_position(
            "/MES",
            opened_at=last_date - timedelta(days=30),
            quantity=3,
            avg_cost=Decimal("110"),
        )
        result = strategy.generate_exit_candidates(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        sig = result.signals[0]
        assert sig.prior_position_direction is Direction.LONG
        assert sig.prior_position_quantity == 3

    def test_decommission_falls_back_to_avg_cost_when_universe_missing(self) -> None:
        """Decommission corner case: the market has been removed from
        ``active_universe`` (delisted / suspended) but a position is still
        held. The CLOSE candidate must still emit; decision_price falls
        back to ``position.avg_cost`` per _build_exit_candidate."""
        strat = _strategy_with_decommission(decommissioned=True)
        position = _long_position(
            "/MES",
            opened_at=date(2026, 1, 1),
            avg_cost=Decimal("112.50"),
        )
        result = strat.generate_exit_candidates(
            active_universe={},  # market dropped from universe
            current_positions={"/MES": position},
            as_of_session_date=date(2026, 5, 1),
        )
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.exit_reason == "decommission"
        assert sig.decision_price == Decimal("112.50")

    def test_emitted_at_default_now_utc(self, strategy: V1TrendFollowing) -> None:
        """When ``as_of_emitted_at_utc`` is not provided the result records
        a wall-clock UTC timestamp. Locks the default-to-now behavior."""
        before = datetime.now(tz=UTC)
        result = strategy.generate_exit_candidates(
            active_universe={},
            current_positions={},
            as_of_session_date=date(2026, 5, 1),
        )
        after = datetime.now(tz=UTC)
        assert before <= result.as_of_emitted_at_utc <= after


# ---------------------------------------------------------------------------
# Entry-side STRATEGY_DECOMMISSIONED short-circuit
# ---------------------------------------------------------------------------
class TestEntryPipelineDecommissioned:
    """Under ``strategy_decommissioned=True`` the entry pipeline must emit
    no candidates AND record a STRATEGY_DECOMMISSIONED rejection per
    market in the active universe. This prevents the "candidate emitted
    that can never fill" anti-pattern (see design §7 interaction table)."""

    def test_decommissioned_rejects_every_market(self) -> None:
        strat = _strategy_with_decommission(decommissioned=True)
        series = _trending_series("/MES")
        result = strat.generate_signals(
            active_universe={"/MES": series},
            current_positions={},
            as_of_session_date=series.bars[-1].session_date,
        )
        assert result.signals == ()
        assert result.rejections == (("/MES", RejectionReason.STRATEGY_DECOMMISSIONED),)

    def test_decommissioned_short_circuits_before_history_check(self) -> None:
        """Step 0 (STRATEGY_DECOMMISSIONED) runs BEFORE step 1
        (INSUFFICIENT_BAR_HISTORY). A short series under decommission
        records STRATEGY_DECOMMISSIONED, not INSUFFICIENT_BAR_HISTORY —
        verifying the short-circuit ordering."""
        strat = _strategy_with_decommission(decommissioned=True)
        short_series = BarSeries(
            market="/MES",
            bars=tuple(
                _make_bar(date(2026, 1, 1) + timedelta(days=i), close=Decimal("100"))
                for i in range(5)
            ),
        )
        result = strat.generate_signals(
            active_universe={"/MES": short_series},
            current_positions={},
            as_of_session_date=short_series.bars[-1].session_date,
        )
        assert result.rejections == (("/MES", RejectionReason.STRATEGY_DECOMMISSIONED),)


# ---------------------------------------------------------------------------
# parameters.py — exit-pipeline fields
# ---------------------------------------------------------------------------
class TestExitPipelineParameters:
    def test_defaults_are_safe(self) -> None:
        """Both new parameters default to False at launch (per design L4/L6).
        Flipping either is an operator-only decision."""
        params = default_v1_parameters()
        assert params.strategy_decommissioned is False
        assert params.exit_auto_approve is False

    def test_canonical_dict_includes_new_keys(self) -> None:
        """STRATEGY_DECOMMISSIONED and EXIT_AUTO_APPROVE must appear in the
        canonical dict so parameter_set_hash captures them — flipping
        either is a parameter change that must be auditable."""
        canonical = default_v1_parameters().to_canonical_dict()
        assert canonical["STRATEGY_DECOMMISSIONED"] == "False"
        assert canonical["EXIT_AUTO_APPROVE"] == "False"

    def test_explicit_true_serializes_canonically(self) -> None:
        params = dataclasses.replace(
            default_v1_parameters(),
            strategy_decommissioned=True,
            exit_auto_approve=True,
        )
        canonical = params.to_canonical_dict()
        assert canonical["STRATEGY_DECOMMISSIONED"] == "True"
        assert canonical["EXIT_AUTO_APPROVE"] == "True"

    def test_v1parameters_accepts_kwargs(self) -> None:
        """Sanity: constructing V1Parameters with the new fields as kwargs
        works (avoids positional surprises if a future param is inserted
        between the existing tail and the new fields)."""
        params = V1Parameters(
            lookback_days_donchian=60,
            ma_fast_days=50,
            ma_slow_days=200,
            efficiency_ratio_threshold=Decimal("0.20"),
            stop_distance_atr_mult=Decimal("3.0"),
            atr_lookback_days=20,
            min_holding_days=14,
            vol_target_pct_annual=Decimal("0.15"),
            instrument_vol_lookback_days=60,
            roll_days_before_expiry=5,
            strategy_decommissioned=True,
            exit_auto_approve=True,
        )
        assert params.strategy_decommissioned is True
        assert params.exit_auto_approve is True
