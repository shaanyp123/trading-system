"""V1TrendFollowing — Donchian / MA / Hurst trend-following strategy (Phase 1).

Implements the strategy logic locked in `Docs/backend-spec.md §2.3`:

- Entry signal: Donchian channel breakout (LOOKBACK_DAYS_DONCHIAN-day high to
  upside / low to downside) AND trend filter (close > MA_FAST > MA_SLOW for long;
  inverted for short) AND Hurst >= HURST_THRESHOLD over the same lookback.
- Stop: ATR-based, STOP_DISTANCE_ATR_MULT x ATR(20). Stop-market exit. Strategy
  emits the `stop_price` as a candidate; execution service places the actual
  stop order.
- Profit target: none. Exit only on stop hit, signal reversal, MIN_HOLDING_DAYS
  satisfied AND trend filter flips, or strategy decommission. Strategy emits
  reversal/exit candidates; execution actuates.
- Sizing: delegated entirely to `services/risk/sizing.py` (Stage 0-5). Strategy
  outputs `CandidateSignal` with `decision_price` + `stop_price` + indicator
  snapshot; risk engine fills `target_contracts` + `sizing_trace`.

Day 2 skeleton: implements the entry-signal pipeline + rejection codes + audit
event payload assembly. Exit handling (stop, reversal, min-holding) is
scaffolded with `NotImplementedError` because:
1. It depends on broker position state via `Position` snapshots, which the
   QC adapter delivers (Phase 0 Week 4-5 deliverable).
2. The exit decision interacts with the kill-switch state machine (HALT_NEW
   blocks new entries but allows exits) — that interaction lives in the signal
   service, not here.
The `generate_exit_candidates` API surface is declared so the QC LEAN wrapper
and the signal service can both call it once the rest of the system catches up;
the v1 PR adding exit logic will land in Week 3-4.

Strategy version identity (`strategy_hash`) is the git SHA of the head commit
when this code is loaded. Computation is in `services/version/composite_hash.py`,
not here — the strategy is name-and-logic only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

import structlog

from strategies.v1_trend_following.indicators import (
    average_true_range,
    donchian_channel,
    hurst_exponent_rs,
    simple_moving_average,
)
from strategies.v1_trend_following.parameters import V1Parameters
from strategies.v1_trend_following.signals import (
    Bar,
    BarSeries,
    CandidateSignal,
    Direction,
    Position,
    RejectionReason,
    SignalGenerationResult,
)

STRATEGY_NAME: Final[str] = "v1_trend_following"

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class _IndicatorSnapshot:
    """Internal record of one market's indicator values for a session."""

    donchian_high: Decimal
    donchian_low: Decimal
    ma_fast: Decimal
    ma_slow: Decimal
    hurst: Decimal
    atr: Decimal
    last_close: Decimal


class V1TrendFollowing:
    """Stateless strategy class. Construct once per process; reuse across sessions.

    Statelessness is by design — every input the strategy needs is passed into
    `generate_signals()`. This makes the strategy trivially parallelizable for
    backtesting and removes a class of bugs where session N state leaks into
    session N+1.
    """

    def __init__(self, parameters: V1Parameters) -> None:
        self._params = parameters
        # `min_required_bars` is the largest lookback the strategy needs.
        # Donchian and Hurst share the same lookback per backend-spec §2.3
        # ("confirmed by Hurst exponent over the same lookback"). MA_SLOW
        # may exceed it. ATR needs lookback + 1 prior bar.
        self._min_required_bars = max(
            parameters.lookback_days_donchian,
            parameters.ma_slow_days,
            parameters.atr_lookback_days + 1,
            parameters.instrument_vol_lookback_days,  # for Stage 1 sigma; not strictly used here
        )

    @property
    def parameters(self) -> V1Parameters:
        return self._params

    @property
    def min_required_bars(self) -> int:
        return self._min_required_bars

    # ------------------------------------------------------------------
    # Entry-signal pipeline
    # ------------------------------------------------------------------
    def generate_signals(
        self,
        *,
        active_universe: Mapping[str, BarSeries],
        current_positions: Mapping[str, Position],
        as_of_session_date: date,
        as_of_emitted_at_utc: datetime | None = None,
    ) -> SignalGenerationResult:
        """Run the entry-signal pipeline over the active universe.

        `active_universe` is the OUTPUT of `services/risk/sizing.py` Stage 0
        (markets that pass the 50%-single-contract-notional filter at current
        equity). The strategy does not run Stage 0 itself.

        Each market is independently evaluated. A market is either:
        - emitted as a `CandidateSignal` (passed all filters), OR
        - recorded in `rejections` with a `RejectionReason`.

        No market appears in both. The signal service consumes the result and
        emits `signal_emitted` / `signal_rejected` audit events accordingly.
        """
        emitted_at = as_of_emitted_at_utc or datetime.now(tz=UTC)
        signals: list[CandidateSignal] = []
        rejections: list[tuple[str, RejectionReason]] = []

        for market, series in active_universe.items():
            if series.market != market:
                # Defensive: caller-provided dict key must match the series' own market.
                # This is a programming error, not a strategy decision.
                raise ValueError(
                    f"active_universe key {market!r} != BarSeries.market {series.market!r}"
                )
            try:
                signal_or_reject = self._evaluate_market(
                    market=market,
                    series=series,
                    position=current_positions.get(market),
                    as_of_session_date=as_of_session_date,
                )
            except ValueError as exc:
                # Indicator computation failed (insufficient bars, degenerate
                # regression, etc.). Treat as INSUFFICIENT_BAR_HISTORY unless
                # the message says otherwise; sentinel logging captures the
                # exact reason for forensic review.
                log.warning(
                    "v1_indicator_compute_failed",
                    market=market,
                    session_date=str(as_of_session_date),
                    error=str(exc),
                )
                rejections.append((market, RejectionReason.INSUFFICIENT_BAR_HISTORY))
                continue

            if isinstance(signal_or_reject, CandidateSignal):
                signals.append(signal_or_reject)
            else:
                rejections.append((market, signal_or_reject))

        return SignalGenerationResult(
            signals=tuple(signals),
            rejections=tuple(rejections),
            as_of_emitted_at_utc=emitted_at,
        )

    def _evaluate_market(
        self,
        *,
        market: str,
        series: BarSeries,
        position: Position | None,
        as_of_session_date: date,
    ) -> CandidateSignal | RejectionReason:
        """Per-market entry-signal evaluation. Returns a CandidateSignal on
        passing all filters, or a RejectionReason on failure.

        Order of checks matches backend-spec §2.3:
          1. enough history?              -> INSUFFICIENT_BAR_HISTORY
          2. Donchian breakout?           -> NO_BREAKOUT
          3. trend filter passes?         -> TREND_FILTER_FAILED
          4. Hurst >= threshold?          -> HURST_BELOW_THRESHOLD
          5. min-holding-days satisfied?  -> MIN_HOLDING_DAYS_NOT_SATISFIED
        """
        bars = series.bars
        if len(bars) < self._min_required_bars:
            return RejectionReason.INSUFFICIENT_BAR_HISTORY

        snapshot = self._compute_snapshot(bars)
        last_close = snapshot.last_close

        # Step 2: Donchian breakout direction.
        breakout: Direction
        if last_close > snapshot.donchian_high:
            breakout = Direction.LONG
        elif last_close < snapshot.donchian_low:
            breakout = Direction.SHORT
        else:
            return RejectionReason.NO_BREAKOUT

        # Step 3: trend filter — close above (long) or below (short) the MA pair,
        # with MA_FAST also above (long) or below (short) MA_SLOW.
        if breakout is Direction.LONG:
            if not (last_close > snapshot.ma_fast > snapshot.ma_slow):
                return RejectionReason.TREND_FILTER_FAILED
        else:  # SHORT
            if not (last_close < snapshot.ma_fast < snapshot.ma_slow):
                return RejectionReason.TREND_FILTER_FAILED

        # Step 4: Hurst persistence threshold (same direction-agnostic check —
        # Hurst measures persistence regardless of sign).
        if snapshot.hurst < self._params.hurst_threshold:
            return RejectionReason.HURST_BELOW_THRESHOLD

        # Step 5: MIN_HOLDING_DAYS check — only matters if the market has an
        # OPEN position in the OPPOSITE direction (a reversal). Same-direction
        # already-long signals are dedup'd by the signal service, not here.
        if position is not None and position.direction is not Direction.FLAT:
            same_direction = position.direction is breakout
            if not same_direction and position.opened_at_session_date is not None:
                held = (as_of_session_date - position.opened_at_session_date).days
                if held < self._params.min_holding_days:
                    return RejectionReason.MIN_HOLDING_DAYS_NOT_SATISFIED

        # All filters passed. Compute stop and emit candidate.
        stop_price = self._compute_stop_price(
            direction=breakout, decision_price=last_close, atr=snapshot.atr
        )
        return CandidateSignal(
            market=market,
            direction=breakout,
            signal_type="donchian_breakout",
            session_date=as_of_session_date,
            decision_price=last_close,
            stop_price=stop_price,
            indicators_snapshot={
                "donchian_high": snapshot.donchian_high,
                "donchian_low": snapshot.donchian_low,
                "ma_fast": snapshot.ma_fast,
                "ma_slow": snapshot.ma_slow,
                "hurst": snapshot.hurst,
                "atr": snapshot.atr,
                "lookback_days_donchian": self._params.lookback_days_donchian,
            },
        )

    def _compute_snapshot(self, bars: tuple[Bar, ...]) -> _IndicatorSnapshot:
        """Compute all indicators required for the entry decision."""
        params = self._params
        closes = tuple(b.close for b in bars)
        # Donchian uses today's bar in the lookback per backend-spec §2.3
        # ("LOOKBACK_DAYS_DONCHIAN-day high broken to upside"). The breakout check
        # then compares last_close against the channel; the high must therefore
        # be computed over bars[-lookback:] inclusive.
        channel = donchian_channel(bars, params.lookback_days_donchian)
        ma_fast = simple_moving_average(closes, params.ma_fast_days)
        ma_slow = simple_moving_average(closes, params.ma_slow_days)
        hurst = hurst_exponent_rs(closes[-params.lookback_days_donchian :])
        atr = average_true_range(bars, params.atr_lookback_days)
        return _IndicatorSnapshot(
            donchian_high=channel.high,
            donchian_low=channel.low,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
            hurst=hurst,
            atr=atr,
            last_close=closes[-1],
        )

    def _compute_stop_price(
        self,
        *,
        direction: Direction,
        decision_price: Decimal,
        atr: Decimal,
    ) -> Decimal:
        offset = atr * self._params.stop_distance_atr_mult
        if direction is Direction.LONG:
            return decision_price - offset
        if direction is Direction.SHORT:
            return decision_price + offset
        raise ValueError(f"_compute_stop_price: unexpected direction {direction!r}")

    # ------------------------------------------------------------------
    # Exit-signal pipeline (scaffolded; full logic lands Week 3-4)
    # ------------------------------------------------------------------
    def generate_exit_candidates(
        self,
        *,
        active_universe: Mapping[str, BarSeries],
        current_positions: Mapping[str, Position],
        as_of_session_date: date,
    ) -> SignalGenerationResult:
        """Exit signal pipeline.

        Per backend-spec §2.3, exit conditions for a held position are:
          (a) stop hit (handled by execution service watching the stop-market order)
          (b) signal reversal (Donchian breakout in opposite direction; uses
              `generate_signals` output)
          (c) MIN_HOLDING_DAYS satisfied AND trend filter flips
          (d) strategy decommission (orchestrated outside the strategy module)

        Day 2 status: scaffolded. Implementation lands in Week 3-4 with the
        signal service end-to-end (implementation-guide §3 Week 3). Inputs and
        return type are stable; callers can rely on the API surface.
        """
        del active_universe, current_positions, as_of_session_date
        raise NotImplementedError(
            "V1TrendFollowing.generate_exit_candidates is scaffolded for Week 3-4. "
            "Day 2 ships the entry pipeline only."
        )
