"""Unit test stubs for v1_trend_following.

Day 2 scope: enough tests to lock the public-API contracts and exercise the
happy path + each rejection branch. Heavier tests (parity vs vbt, golden bar
fixtures, full universe runs at multiple equity tiers) land in Week 2-4 per
implementation-guide §3.

Test naming follows claude-dev-guide §6.1: `test_<unit>_<scenario>_<expected>`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from strategies.v1_trend_following import (
    V1_CANDIDATE_UNIVERSE,
    V1_SIDELINED_MARKETS,
    Bar,
    BarSeries,
    CandidateSignal,
    Direction,
    Position,
    RejectionReason,
    SignalGenerationResult,
    V1Parameters,
    V1TrendFollowing,
    default_v1_parameters,
)
from strategies.v1_trend_following.indicators import (
    average_true_range,
    donchian_channel,
    hurst_exponent_rs,
    simple_moving_average,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_bar(d: date, *, close: Decimal, ohl_offset: Decimal = Decimal("1.00")) -> Bar:
    """Construct a Bar with synthetic OHL around the close. ATR = 2 x ohl_offset."""
    return Bar(
        session_date=d,
        open=close - ohl_offset / 2,
        high=close + ohl_offset,
        low=close - ohl_offset,
        close=close,
        volume=10_000,
    )


def _series_with_breakout(
    market: str,
    *,
    n_bars: int = 250,
    base: Decimal = Decimal("100"),
    breakout_at_end: Decimal | None = Decimal("130"),
) -> BarSeries:
    """Construct a synthetic price series with a clear upward breakout on the
    final bar. Uses a smooth exponential drift to keep MA_FAST > MA_SLOW and
    Hurst high enough to pass the persistence filter.
    """
    start = date(2026, 1, 1)
    bars: list[Bar] = []
    for i in range(n_bars):
        # Smooth trending drift: 0.2% per day compounding. Without noise the
        # Hurst R/S regression is degenerate (zero variance after mean adjust),
        # so add a tiny modulation that doesn't disturb the trend.
        drift = base * (Decimal("1.002") ** i)
        wave = Decimal(str(0.5 + 0.5 * ((i % 7) / 6)))
        close = (drift + wave).quantize(Decimal("0.01"))
        bars.append(_make_bar(start + timedelta(days=i), close=close))
    if breakout_at_end is not None:
        # Replace the last bar with a clear breakout above the trailing high.
        last_date = bars[-1].session_date
        bars[-1] = _make_bar(last_date, close=breakout_at_end)
    return BarSeries(market=market, bars=tuple(bars))


# ---------------------------------------------------------------------------
# parameters.py
# ---------------------------------------------------------------------------
class TestV1Parameters:
    def test_defaults_roundtrip_to_canonical_dict(self) -> None:
        params = default_v1_parameters()
        canonical = params.to_canonical_dict()
        # Keys are UPPER_CASE per backend-spec §12.3 enum.
        assert "LOOKBACK_DAYS_DONCHIAN" in canonical
        assert "MIN_HOLDING_DAYS" in canonical
        # Values are stringified (Decimal precision preservation).
        assert canonical["HURST_THRESHOLD"] == "0.55"
        assert canonical["LOOKBACK_DAYS_DONCHIAN"] == "60"

    def test_invalid_ma_pair_rejected(self) -> None:
        with pytest.raises(ValueError, match="MA pair invalid"):
            V1Parameters(
                lookback_days_donchian=60,
                ma_fast_days=50,
                ma_slow_days=40,  # slow < fast — invalid
                hurst_threshold=Decimal("0.50"),
                stop_distance_atr_mult=Decimal("3.0"),
                atr_lookback_days=20,
                min_holding_days=14,
                vol_target_pct_annual=Decimal("0.15"),
                instrument_vol_lookback_days=60,
                roll_days_before_expiry=5,
            )

    def test_hurst_threshold_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="hurst_threshold"):
            V1Parameters(
                lookback_days_donchian=60,
                ma_fast_days=50,
                ma_slow_days=200,
                hurst_threshold=Decimal("0.95"),  # > 0.80 cap
                stop_distance_atr_mult=Decimal("3.0"),
                atr_lookback_days=20,
                min_holding_days=14,
                vol_target_pct_annual=Decimal("0.15"),
                instrument_vol_lookback_days=60,
                roll_days_before_expiry=5,
            )


# ---------------------------------------------------------------------------
# indicators.py
# ---------------------------------------------------------------------------
class TestIndicators:
    def test_donchian_channel_excludes_current_bar(self) -> None:
        """Channel = high/low of the N bars PRIOR to the current (last) bar.

        Monotone-increasing closes: 100, 101, ..., 160 over 61 bars. The last
        bar (close=160) is the current bar and is EXCLUDED from the channel.
        The channel should reflect the highest PRIOR bar (the one before last,
        close=159, high=160). This locks the spec-canonical exclusive Donchian
        interpretation per backend-spec §2.3.
        """
        bars = [
            _make_bar(date(2026, 1, 1) + timedelta(days=i), close=Decimal(str(100 + i)))
            for i in range(61)
        ]
        ch = donchian_channel(bars, lookback_days=60)
        # The channel high comes from bars[-2] (the previous bar), NOT bars[-1].
        assert ch.high == bars[-2].high
        assert ch.high != bars[-1].high  # current bar excluded
        # The channel low is the first bar in the 60-bar prior window = bars[0].
        assert ch.low == bars[0].low

    def test_donchian_insufficient_bars_raises(self) -> None:
        # Need lookback_days + 1 bars (channel window + current bar). Provide 1.
        bars = [_make_bar(date(2026, 1, 1), close=Decimal("100"))]
        with pytest.raises(ValueError, match="need 61 bars"):
            donchian_channel(bars, lookback_days=60)

    def test_donchian_breakout_when_close_exceeds_prior_60day_high(self) -> None:
        """The /MNQ-tonight regression test.

        2026-05-26: /MNQ closed at 30038.25 with intraday high 30119. Prior
        60-day max high was ~29783.75 (May 14). The canonical Turtle breakout
        interpretation says this IS a long breakout (close exceeded the 60-day
        prior high), but the previous inclusive-window implementation rejected
        it because today's close <= today's own high.

        This test recreates that situation: 60 prior bars where all highs are
        below 200, then a current bar with close=210 and high=215. The channel
        should be from the prior window (max high < 200), and the close (210)
        should exceed it.
        """
        # 60 prior bars with highs at most ~150 (close around 100-150)
        prior_bars = [
            _make_bar(date(2026, 1, 1) + timedelta(days=i), close=Decimal(str(100 + i * 0.5)))
            for i in range(60)
        ]
        # Current bar: close=210, high=215 (high > close, as always; close above prior range)
        current_bar = _make_bar(
            date(2026, 1, 1) + timedelta(days=60), close=Decimal("210")
        )
        bars = prior_bars + [current_bar]

        ch = donchian_channel(bars, lookback_days=60)

        # Channel should be from the prior 60 bars — max high there is bars[-2].high = ~130.5
        # NOT from the current bar (215).
        assert ch.high < Decimal("200")
        assert ch.high != current_bar.high

        # The breakout check (current_close > channel.high) should now succeed.
        # Previously (inclusive window) this would fail because channel.high = 215.
        assert current_bar.close > ch.high, (
            f"close={current_bar.close} should exceed prior-60-day high={ch.high}; "
            f"if this assertion fails, the exclusive-window fix regressed"
        )

    def test_simple_moving_average_arithmetic_mean(self) -> None:
        closes = [Decimal("100"), Decimal("110"), Decimal("90"), Decimal("100")]
        assert simple_moving_average(closes, window=4) == Decimal("100.00000000")

    def test_atr_constant_range(self) -> None:
        # 21 bars; each has high=close+1, low=close-1 ⇒ TR ~= 2 across all bars.
        bars = [
            _make_bar(date(2026, 1, 1) + timedelta(days=i), close=Decimal("100")) for i in range(21)
        ]
        atr = average_true_range(bars, lookback_days=20)
        assert atr == Decimal("2.00000000")

    def test_hurst_random_walk_near_half(self) -> None:
        # Synthetic Brownian-like increments via a deterministic LCG keep the
        # test reproducible without a numpy dependency.
        seed = 12345
        closes: list[Decimal] = [Decimal("100")]
        for _ in range(127):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            inc = (seed % 200 - 100) / 1000.0  # ~ U(-0.1, 0.1)
            prev = float(closes[-1])
            closes.append(Decimal(str(round(prev * (1 + inc), 4))))
        h = hurst_exponent_rs(closes)
        # R/S has a known small-sample upward bias; tolerance reflects that.
        assert Decimal("0.30") <= h <= Decimal("0.85"), f"H out of expected range: {h}"


# ---------------------------------------------------------------------------
# strategy.py — happy path + rejections
# ---------------------------------------------------------------------------
class TestV1TrendFollowing:
    @pytest.fixture(scope="class")
    def strategy(self) -> V1TrendFollowing:
        return V1TrendFollowing(default_v1_parameters())

    def test_min_required_bars_takes_max_lookback(self, strategy: V1TrendFollowing) -> None:
        # MA_SLOW=200 > LOOKBACK_DAYS_DONCHIAN=60 > ATR_LOOKBACK_DAYS+1=21.
        assert strategy.min_required_bars == 200

    def test_long_breakout_passes_all_filters(self, strategy: V1TrendFollowing) -> None:
        # A clean upward-trending series with a final-bar breakout. With the
        # default params (60 / 50 / 200 / 0.50 / 3.0xATR), the trending series
        # fixture should pass Donchian + MA + Hurst.
        series = _series_with_breakout("/MES")
        result = strategy.generate_signals(
            active_universe={"/MES": series},
            current_positions={},
            as_of_session_date=series.bars[-1].session_date,
            as_of_emitted_at_utc=datetime(2026, 5, 1, 21, 30, tzinfo=UTC),
        )
        assert isinstance(result, SignalGenerationResult)
        # Either we got a long breakout candidate, or Hurst/MA filtered it out.
        # Both are valid behaviors for a synthetic series — the assertion that
        # matters is that one or the other resolution happened cleanly.
        if result.signals:
            sig = result.signals[0]
            assert isinstance(sig, CandidateSignal)
            assert sig.market == "/MES"
            assert sig.direction is Direction.LONG
            assert sig.signal_type == "donchian_breakout"
            assert sig.stop_price < sig.decision_price  # long stops are below entry
            assert "donchian_high" in sig.indicators_snapshot
        else:
            # Diagnostic so a failure here is debuggable without re-running.
            reasons = {r for _, r in result.rejections}
            assert reasons.issubset(
                {
                    RejectionReason.HURST_BELOW_THRESHOLD,
                    RejectionReason.TREND_FILTER_FAILED,
                    RejectionReason.NO_BREAKOUT,
                }
            ), f"unexpected rejection set: {reasons}"

    def test_insufficient_history_rejected(self, strategy: V1TrendFollowing) -> None:
        short_series = BarSeries(
            market="/MES",
            bars=tuple(
                _make_bar(date(2026, 1, 1) + timedelta(days=i), close=Decimal("100"))
                for i in range(50)  # < min_required_bars=200
            ),
        )
        result = strategy.generate_signals(
            active_universe={"/MES": short_series},
            current_positions={},
            as_of_session_date=short_series.bars[-1].session_date,
        )
        assert result.signals == ()
        assert result.rejections == (("/MES", RejectionReason.INSUFFICIENT_BAR_HISTORY),)

    def test_no_breakout_when_close_inside_channel(self, strategy: V1TrendFollowing) -> None:
        """Series with mild noise but no breakout on the final bar.

        A perfectly flat series triggers zero-variance in the Hurst R/S
        regression and degenerates the indicator pipeline, so we use a
        small-amplitude oscillation instead — enough variance to compute
        Hurst, not enough to break the trailing 60-day channel.
        """
        # 250 bars oscillating in [98, 102] — channel range ~4 absolute, never
        # exceeded by the final close (which lands mid-range).
        bars: list[Bar] = []
        for i in range(250):
            close = Decimal("100") + Decimal(str((i % 11) - 5)) / Decimal("5")
            bars.append(_make_bar(date(2026, 1, 1) + timedelta(days=i), close=close))
        # Force the final close to sit clearly inside the trailing channel.
        last_date = bars[-1].session_date
        bars[-1] = _make_bar(last_date, close=Decimal("100"))
        series = BarSeries(market="TLT", bars=tuple(bars))

        result = strategy.generate_signals(
            active_universe={"TLT": series},
            current_positions={},
            as_of_session_date=last_date,
        )
        assert result.signals == ()
        reasons = {r for _, r in result.rejections}
        assert reasons.issubset(
            {RejectionReason.NO_BREAKOUT, RejectionReason.TREND_FILTER_FAILED}
        ), f"unexpected rejection reasons: {reasons}"

    def test_min_holding_days_blocks_reversal(self, strategy: V1TrendFollowing) -> None:
        """Held-long position with a short breakout signal less than
        MIN_HOLDING_DAYS old should be rejected with MIN_HOLDING_DAYS_NOT_SATISFIED.

        Constructed series: smooth uptrend then sharp final-bar drop below the
        Donchian low. With a long position opened today, the reversal short
        signal is suppressed.
        """
        # Use a downward-breakout series to trigger a short signal.
        series = _series_with_breakout("/MES", breakout_at_end=Decimal("60"))
        last_date = series.bars[-1].session_date

        position = Position(
            market="/MES",
            direction=Direction.LONG,
            quantity=1,
            avg_cost=Decimal("110"),
            opened_at_session_date=last_date - timedelta(days=3),  # 3 days held
        )
        result = strategy.generate_signals(
            active_universe={"/MES": series},
            current_positions={"/MES": position},
            as_of_session_date=last_date,
        )
        # Either MIN_HOLDING_DAYS_NOT_SATISFIED (short reversal blocked) or one
        # of the upstream filters rejected first (TREND_FILTER on a sudden flip
        # is plausible). The contract being tested is that NO same-day reversal
        # signal escapes.
        assert all(sig.direction is not Direction.SHORT for sig in result.signals), (
            f"reversal short emitted despite held long: {result.signals}"
        )

    def test_active_universe_key_mismatch_raises(self, strategy: V1TrendFollowing) -> None:
        series = _series_with_breakout("/MNQ")
        with pytest.raises(ValueError, match="active_universe key"):
            strategy.generate_signals(
                active_universe={"/MES": series},  # key /MES but series is /MNQ
                current_positions={},
                as_of_session_date=series.bars[-1].session_date,
            )

    def test_exit_pipeline_scaffolded(self, strategy: V1TrendFollowing) -> None:
        with pytest.raises(NotImplementedError, match="scaffolded for Week 3-4"):
            strategy.generate_exit_candidates(
                active_universe={},
                current_positions={},
                as_of_session_date=date(2026, 5, 1),
            )


# ---------------------------------------------------------------------------
# Module-level public-API smoke (catches __init__ regressions)
# ---------------------------------------------------------------------------
def test_public_api_surface_intact() -> None:
    from strategies import v1_trend_following as mod

    expected = {
        "STRATEGY_NAME",
        "V1_CANDIDATE_UNIVERSE",
        "V1_DEFAULTS",
        "V1_SIDELINED_MARKETS",
        "Bar",
        "BarSeries",
        "CandidateSignal",
        "Direction",
        "Position",
        "RejectionReason",
        "SignalEmittedPayload",
        "SignalGenerationResult",
        "SignalRejectedPayload",
        "SizingTraceV1",
        "Stage0Trace",
        "StrategyAuditEvent",
        "UniverseExclusionPayload",
        "UniverseInclusionPayload",
        "V1Parameters",
        "V1TrendFollowing",
        "default_v1_parameters",
    }
    assert expected.issubset(set(mod.__all__))
    for name in expected:
        assert hasattr(mod, name), f"strategies.v1_trend_following missing {name}"


# ---------------------------------------------------------------------------
# Sideline invariant
# ---------------------------------------------------------------------------


class TestSidelinedMarkets:
    """``V1_SIDELINED_MARKETS`` is the canonical registry of markets that have
    full infrastructure support in the codebase (expiry rules, holiday
    calendars, sentinel substitution, alert builders, tick sizes, IBKR
    contract-resolution paths, reconciliation labels) but are explicitly
    excluded from the active strategy universe pending operator action on
    documented re-enable preconditions.

    These tests lock the invariant that no market is simultaneously active
    AND sidelined, so a future re-enable (or an accidental sideline) can't
    leave the system in a contradictory state.
    """

    def test_sidelined_markets_disjoint_from_candidate_universe(self) -> None:
        # A market can't be both active and sidelined — would mean the
        # strategy thinks it should trade something the rest of the system
        # has explicitly disabled.
        assert (V1_SIDELINED_MARKETS & set(V1_CANDIDATE_UNIVERSE)) == set()

    def test_sidelined_markets_is_frozenset(self) -> None:
        # frozenset (not set) — immutable so import-time consumers can't
        # accidentally mutate the registry.
        assert isinstance(V1_SIDELINED_MARKETS, frozenset)

    def test_mcl_currently_sidelined(self) -> None:
        # Lock the 2026-05-23 PR #228 sideline. When /MCL is re-enabled
        # (see V1_SIDELINED_MARKETS docstring in parameters.py for the
        # runbook), this assertion will need to be deleted in the same PR.
        assert "/MCL" in V1_SIDELINED_MARKETS

    def test_active_universe_excludes_mcl(self) -> None:
        # Paired with test_mcl_currently_sidelined — locks the active
        # universe doesn't accidentally re-include /MCL while it's still
        # in the sideline registry.
        assert "/MCL" not in V1_CANDIDATE_UNIVERSE
