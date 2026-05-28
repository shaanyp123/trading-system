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

Day 2 shipped the entry-signal pipeline + rejection codes + audit
event payload assembly. PR-A of `Docs/exit-pipeline-design.md` (2026-05-26)
adds the strategy-side exit pipeline: `generate_exit_candidates` evaluates
(b) reversal, (c) trend-flip, and (d) decommission per market and emits at
most one CLOSE candidate per held position per cycle. (a) stop-hit is
unchanged — it stays handled by the bracket stop-market at IBKR. The
strategy is broker-agnostic and pure-policy: the LEAN wrapper +
trigger_v1_cycle (PR-B) and the dispatcher / order_placement_worker
(PR-C) consume the new ExitGenerationResult and actuate it.

Strategy version identity (`strategy_hash`) is the git SHA of the head commit
when this code is loaded. Computation is in `services/version/composite_hash.py`,
not here — the strategy is name-and-logic only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, Literal

import structlog

from strategies.v1_trend_following.indicators import (
    average_true_range,
    donchian_channel,
    hurst_exponent_rs,
    simple_moving_average,
)
from strategies.v1_trend_following.parameters import V1Parameters
from strategies.v1_trend_following.proximity import (
    MarketProximity,
    compute_market_proximity,
)
from strategies.v1_trend_following.signals import (
    Bar,
    BarSeries,
    CandidateSignal,
    Direction,
    ExitGenerationResult,
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


@dataclass(frozen=True, slots=True)
class _EvaluationResult:
    """Internal return wrapper for ``_evaluate_market``.

    ``outcome`` is the unchanged ``CandidateSignal | RejectionReason`` that
    the gate-evaluation logic produces. ``snapshot`` is the indicator snapshot
    used in the evaluation, exposed up to ``generate_signals`` so the
    proximity record (Docs/signal-proximity-design.md §4.3) can be built
    without re-computing indicators. ``snapshot`` is None when the gate
    pipeline short-circuited BEFORE computing the snapshot (decommission or
    insufficient bars). The gate logic itself is bit-identical to the pre-
    proximity implementation; only the return shape changed.
    """

    outcome: CandidateSignal | RejectionReason
    snapshot: _IndicatorSnapshot | None


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
        # may exceed it. ATR needs lookback + 1 prior bar. Donchian needs
        # lookback + 1 bars too (channel from prior N bars + the current bar
        # for the breakout-comparison close).
        self._min_required_bars = max(
            parameters.lookback_days_donchian + 1,
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
        market_evaluations: list[MarketProximity] = []

        for market, series in active_universe.items():
            if series.market != market:
                # Defensive: caller-provided dict key must match the series' own market.
                # This is a programming error, not a strategy decision.
                raise ValueError(
                    f"active_universe key {market!r} != BarSeries.market {series.market!r}"
                )
            snapshot_for_proximity: _IndicatorSnapshot | None
            try:
                eval_result = self._evaluate_market(
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
                snapshot_for_proximity = None
            else:
                if isinstance(eval_result.outcome, CandidateSignal):
                    signals.append(eval_result.outcome)
                else:
                    rejections.append((market, eval_result.outcome))
                snapshot_for_proximity = eval_result.snapshot

            # Proximity emission (PR-A of Docs/signal-proximity-design.md).
            # Observation-only — does NOT influence what fires. Per Q4 of the
            # design doc, decommissioned rows still surface what the gates
            # WOULD have said; if the regular flow didn't compute a snapshot
            # because of the decommission short-circuit, compute one best-
            # effort here for display. Insufficient-bars paths still surface
            # a warming_up record with NULL fields.
            if snapshot_for_proximity is None and self._params.strategy_decommissioned:
                if len(series.bars) >= self._min_required_bars:
                    try:
                        snapshot_for_proximity = self._compute_snapshot(series.bars)
                    except ValueError:
                        snapshot_for_proximity = None
            market_evaluations.append(
                self._build_market_proximity(
                    market=market,
                    snapshot=snapshot_for_proximity,
                )
            )

        return SignalGenerationResult(
            signals=tuple(signals),
            rejections=tuple(rejections),
            as_of_emitted_at_utc=emitted_at,
            market_evaluations=tuple(market_evaluations),
        )

    def _build_market_proximity(
        self,
        *,
        market: str,
        snapshot: _IndicatorSnapshot | None,
    ) -> MarketProximity:
        """Adapter that hands snapshot fields off to ``compute_market_proximity``.

        Pure observation-only — does not feed back into entry-signal logic.
        Lives on the strategy class only so it can read ``self._params`` for
        the Hurst threshold and the decommission flag without leaking those
        into the pure-Python proximity module.
        """
        decommissioned = self._params.strategy_decommissioned
        if snapshot is None:
            return compute_market_proximity(
                market=market,
                last_close=None,
                donchian_high=None,
                donchian_low=None,
                ma_fast=None,
                ma_slow=None,
                hurst_value=None,
                hurst_threshold=self._params.hurst_threshold,
                decommissioned=decommissioned,
            )
        return compute_market_proximity(
            market=market,
            last_close=snapshot.last_close,
            donchian_high=snapshot.donchian_high,
            donchian_low=snapshot.donchian_low,
            ma_fast=snapshot.ma_fast,
            ma_slow=snapshot.ma_slow,
            hurst_value=snapshot.hurst,
            hurst_threshold=self._params.hurst_threshold,
            decommissioned=decommissioned,
        )

    def _evaluate_market(
        self,
        *,
        market: str,
        series: BarSeries,
        position: Position | None,
        as_of_session_date: date,
    ) -> _EvaluationResult:
        """Per-market entry-signal evaluation. Returns an ``_EvaluationResult``
        with ``outcome`` (CandidateSignal | RejectionReason) and ``snapshot``
        (the computed indicator snapshot, or None when the gate pipeline
        short-circuited before computing it). PR-A of
        ``Docs/signal-proximity-design.md`` exposes ``snapshot`` so the
        caller can build a proximity record without recomputing indicators;
        the gate-evaluation logic itself is bit-identical to the prior
        implementation.

        Order of checks matches backend-spec §2.3 (with the decommission
        short-circuit prepended per exit-pipeline-design.md §7):
          0. STRATEGY_DECOMMISSIONED?      -> STRATEGY_DECOMMISSIONED
          1. enough history?               -> INSUFFICIENT_BAR_HISTORY
          2. Donchian breakout?            -> NO_BREAKOUT
          3. trend filter passes?          -> TREND_FILTER_FAILED
          4. Hurst >= threshold?           -> HURST_BELOW_THRESHOLD
          5. position-aware gates:
             - same-direction position?    -> POSITION_ALREADY_SAME_DIRECTION
             - opposite-direction position + held < MIN_HOLDING_DAYS
                                          -> MIN_HOLDING_DAYS_NOT_SATISFIED
        """
        # Step 0: decommission short-circuit. When the operator flips
        # ``STRATEGY_DECOMMISSIONED=True``, the entry pipeline emits no
        # candidates for any market. The exit pipeline emits a CLOSE
        # candidate (exit_reason='decommission') for every held position
        # regardless of indicator state. This avoids the "candidate emitted
        # that can never fill" anti-pattern (see design §7 interaction
        # table). Rejection is recorded so the audit log shows the
        # short-circuit decision per market.
        if self._params.strategy_decommissioned:
            return _EvaluationResult(outcome=RejectionReason.STRATEGY_DECOMMISSIONED, snapshot=None)

        bars = series.bars
        if len(bars) < self._min_required_bars:
            return _EvaluationResult(
                outcome=RejectionReason.INSUFFICIENT_BAR_HISTORY, snapshot=None
            )

        snapshot = self._compute_snapshot(bars)
        last_close = snapshot.last_close

        # Step 2: Donchian breakout direction.
        breakout: Direction
        if last_close > snapshot.donchian_high:
            breakout = Direction.LONG
        elif last_close < snapshot.donchian_low:
            breakout = Direction.SHORT
        else:
            return _EvaluationResult(outcome=RejectionReason.NO_BREAKOUT, snapshot=snapshot)

        # Step 3: trend filter — close above (long) or below (short) the MA pair,
        # with MA_FAST also above (long) or below (short) MA_SLOW.
        if breakout is Direction.LONG:
            if not (last_close > snapshot.ma_fast > snapshot.ma_slow):
                return _EvaluationResult(
                    outcome=RejectionReason.TREND_FILTER_FAILED, snapshot=snapshot
                )
        else:  # SHORT
            if not (last_close < snapshot.ma_fast < snapshot.ma_slow):
                return _EvaluationResult(
                    outcome=RejectionReason.TREND_FILTER_FAILED, snapshot=snapshot
                )

        # Step 4: Hurst persistence threshold (same direction-agnostic check —
        # Hurst measures persistence regardless of sign).
        if snapshot.hurst < self._params.hurst_threshold:
            return _EvaluationResult(
                outcome=RejectionReason.HURST_BELOW_THRESHOLD, snapshot=snapshot
            )

        # Step 5: position-aware rejections.
        #
        # Same-direction breakout while we already hold the position →
        # POSITION_ALREADY_SAME_DIRECTION. V1 treats each breakout as ONE
        # entry; subsequent same-direction breakouts are NOT additional
        # entries because V1 has no explicit pyramiding rules (no add-on
        # thresholds in ATR units, no max-units cap). Without this gate
        # every continuation day would emit another candidate that the
        # operator manually rejects in /signals, and in production that
        # becomes accidental scaling. Exit the existing position via
        # stop / reversal / MIN_HOLDING_DAYS-satisfied trend-flip — not
        # via this signal pipeline.
        #
        # Opposite-direction breakout (a reversal) while we hold a position
        # → MIN_HOLDING_DAYS_NOT_SATISFIED if held duration < the
        # configured floor. Otherwise fall through and emit the reversal
        # candidate (the existing position will be closed downstream via
        # the dispatch / fill / reversal-handler chain — out of scope for
        # this entry pipeline).
        if position is not None and position.direction is not Direction.FLAT:
            same_direction = position.direction is breakout
            if same_direction:
                return _EvaluationResult(
                    outcome=RejectionReason.POSITION_ALREADY_SAME_DIRECTION, snapshot=snapshot
                )
            if position.opened_at_session_date is not None:
                held = (as_of_session_date - position.opened_at_session_date).days
                if held < self._params.min_holding_days:
                    return _EvaluationResult(
                        outcome=RejectionReason.MIN_HOLDING_DAYS_NOT_SATISFIED,
                        snapshot=snapshot,
                    )

        # All filters passed. Compute stop and emit candidate.
        stop_price = self._compute_stop_price(
            direction=breakout, decision_price=last_close, atr=snapshot.atr
        )
        return _EvaluationResult(
            outcome=CandidateSignal(
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
            ),
            snapshot=snapshot,
        )

    def _compute_snapshot(self, bars: tuple[Bar, ...]) -> _IndicatorSnapshot:
        """Compute all indicators required for the entry decision."""
        params = self._params
        closes = tuple(b.close for b in bars)
        # Donchian channel = high/low over the N PRIOR days (exclusive of the
        # current bar), per backend-spec §2.3 "N-day high broken to upside, low
        # broken to downside" — canonical Turtle interpretation. The breakout
        # check below compares last_close against this prior-window channel.
        # See indicators.donchian_channel docstring for the rationale on why
        # inclusive-of-current would be mathematically impossible to fire.
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
    # Exit-signal pipeline (PR-A of exit-pipeline-design.md)
    # ------------------------------------------------------------------
    def generate_exit_candidates(
        self,
        *,
        active_universe: Mapping[str, BarSeries],
        current_positions: Mapping[str, Position],
        as_of_session_date: date,
        as_of_emitted_at_utc: datetime | None = None,
        entry_candidates: tuple[CandidateSignal, ...] = (),
    ) -> ExitGenerationResult:
        """Run the exit-signal pipeline over the held positions.

        Per backend-spec §2.3 + exit-pipeline-design.md §3:
          (a) stop hit — unchanged; the bracket stop-market fires at IBKR.
              This pipeline emits no candidate for (a).
          (b) reversal — held position + opposite direction passes ALL 3
              entry filters (Donchian + trend + Hurst). Detected via the
              ``entry_candidates`` argument: the entry pipeline's output is
              the authoritative signal that the opposite direction is "live."
          (c) trend_flip — held LONG + ``close < MA_FAST`` (mirror for SHORT)
              AND held duration >= MIN_HOLDING_DAYS. Per design L2: lazy
              interpretation — ``close < MA_FAST`` only, NOT the full death
              cross.
          (d) decommission — ``STRATEGY_DECOMMISSIONED=True`` forces a CLOSE
              for every held position regardless of indicator state.

        Precedence when a position satisfies more than one condition:
        ``decommission > reversal > trend_flip``. Decommission is the operator
        kill switch and overrides every indicator decision (per L6). Reversal
        is preferred over trend_flip because reversal triggers a paired
        follow-on entry — losing it to trend_flip would emit only the close
        and leave the cycle without the new direction. At most ONE exit
        signal per market per cycle is emitted; the ``exit_reason`` field on
        the candidate captures which condition fired.

        ``entry_candidates`` is the result of the SAME cycle's
        ``generate_signals`` call. The caller (LEAN wrapper or
        trigger_v1_cycle) is expected to invoke ``generate_signals`` first
        and pass the result here; reversal detection couples on it. Passing
        an empty tuple disables reversal detection (only (c) and (d) fire);
        useful for ``--exits-only`` forensic runs and for unit tests that
        exercise (c)/(d) in isolation.

        Returns an ``ExitGenerationResult``. ``signals`` are the emitted
        CLOSE candidates (each with ``signal_type='exit'`` and an
        ``exit_reason`` populated); ``rejections`` are per-market reasons
        no exit fired (TREND_HOLDS, MIN_HOLDING_NOT_REACHED,
        INSUFFICIENT_BAR_HISTORY). Markets with no held position do not
        appear in either tuple — the strategy has nothing to say about them.
        """
        emitted_at = as_of_emitted_at_utc or datetime.now(tz=UTC)
        exit_signals: list[CandidateSignal] = []
        exit_rejections: list[tuple[str, RejectionReason]] = []

        # Pre-compute the set of markets where ``generate_signals`` emitted
        # an opposite-direction breakout. (b) couples on this — the exit
        # pipeline does NOT re-run the entry filters in reverse; it
        # observes the entry pipeline's output. This guarantees the
        # reversal exit fires if and only if all 3 entry filters confirm
        # the opposite direction (per L1: "Donchian-alone is insufficient").
        reversing_entries_by_market: dict[str, CandidateSignal] = {}
        for candidate in entry_candidates:
            held = current_positions.get(candidate.market)
            if held is None or held.direction is Direction.FLAT:
                continue
            if held.direction is candidate.direction:
                continue
            # Held LONG + entry candidate SHORT (or vice versa) =>
            # reversal pair for this market.
            reversing_entries_by_market[candidate.market] = candidate

        for market, position in current_positions.items():
            if position.direction is Direction.FLAT:
                # No held position to exit. The exit pipeline has nothing to
                # say about this market; entry-side logic owns FLAT markets.
                continue

            # --- (d) decommission — highest precedence -----------------
            # Operator kill switch overrides every indicator decision. This
            # check runs before reversal/trend_flip so that a sudden flip
            # of STRATEGY_DECOMMISSIONED while a reversal+trend_flip would
            # have fired still emits a decommission close (operator intent
            # = wind everything down; don't open new opposite positions
            # under decommission via the reversal path).
            if self._params.strategy_decommissioned:
                exit_signals.append(
                    self._build_exit_candidate(
                        market=market,
                        position=position,
                        as_of_session_date=as_of_session_date,
                        exit_reason="decommission",
                        series=active_universe.get(market),
                    )
                )
                continue

            # --- (b) reversal — second precedence ----------------------
            entry_candidate = reversing_entries_by_market.get(market)
            if entry_candidate is not None:
                exit_signals.append(
                    self._build_exit_candidate(
                        market=market,
                        position=position,
                        as_of_session_date=as_of_session_date,
                        exit_reason="reversal",
                        series=active_universe.get(market),
                        paired_entry_market=entry_candidate.market,
                    )
                )
                continue

            # --- (c) trend_flip — third precedence ---------------------
            series = active_universe.get(market)
            if series is None or len(series.bars) < self._min_required_bars:
                # Without enough bars we cannot evaluate the trend-flip
                # condition. Decommission would have fired already; reversal
                # needs an entry-side candidate which itself requires bars
                # to be computed. Falling through here means: no exit, but
                # the operator should know the reason via the audit log.
                exit_rejections.append((market, RejectionReason.INSUFFICIENT_BAR_HISTORY))
                continue

            # MIN_HOLDING_DAYS gate. Matches the entry-side gate at step 5
            # of ``_evaluate_market`` and the backend-spec §2.3 contract:
            # an exit driven by indicator flips waits until the position
            # has had a chance to develop. The floor never blocks
            # decommission (operator override) or reversal (the opposite-
            # direction full-filter confirmation is its own bar for a
            # legitimate exit).
            #
            # If ``opened_at_session_date`` is None we conservatively skip
            # the floor check — mirrors strategy.py:_evaluate_market step 5
            # behavior. This branch is unusual; in production every held
            # position has an opened_at written by the reconciliation path.
            if position.opened_at_session_date is not None:
                held_days = (as_of_session_date - position.opened_at_session_date).days
                if held_days < self._params.min_holding_days:
                    exit_rejections.append((market, RejectionReason.MIN_HOLDING_NOT_REACHED))
                    continue

            try:
                snapshot = self._compute_snapshot(series.bars)
            except ValueError as exc:
                log.warning(
                    "v1_exit_indicator_compute_failed",
                    market=market,
                    session_date=str(as_of_session_date),
                    error=str(exc),
                )
                exit_rejections.append((market, RejectionReason.INSUFFICIENT_BAR_HISTORY))
                continue

            trend_flipped = self._evaluate_trend_flip(
                position_direction=position.direction,
                last_close=snapshot.last_close,
                ma_fast=snapshot.ma_fast,
            )

            if not trend_flipped:
                exit_rejections.append((market, RejectionReason.TREND_HOLDS))
                continue

            exit_signals.append(
                self._build_exit_candidate(
                    market=market,
                    position=position,
                    as_of_session_date=as_of_session_date,
                    exit_reason="trend_flip",
                    series=series,
                    snapshot=snapshot,
                )
            )

        return ExitGenerationResult(
            signals=tuple(exit_signals),
            rejections=tuple(exit_rejections),
            as_of_emitted_at_utc=emitted_at,
        )

    @staticmethod
    def _evaluate_trend_flip(
        *,
        position_direction: Direction,
        last_close: Decimal,
        ma_fast: Decimal,
    ) -> bool:
        """Lazy trend-flip predicate per exit-pipeline-design.md §L2.

        For a held LONG: ``close < MA_FAST`` triggers a flip. For SHORT:
        ``close > MA_FAST`` triggers. This is the "lazy" reading — NOT the
        full death cross (which would also require MA_FAST < MA_SLOW). The
        looser bar exits earlier on weakness; combined with MIN_HOLDING_DAYS
        it strikes the spec's balance between whipsaw protection and
        trend-following timing.

        FLAT direction is impossible at the call site (FLAT markets are
        filtered earlier in ``generate_exit_candidates``); a defensive
        return False here would mask logic errors, so the call site is the
        single source of truth on direction validity.
        """
        if position_direction is Direction.LONG:
            return last_close < ma_fast
        if position_direction is Direction.SHORT:
            return last_close > ma_fast
        # FLAT was filtered above. Defensive False keeps the type checker
        # happy without silently masking a future caller's bug — any FLAT
        # leaking through is by definition not a trend flip.
        return False

    def _build_exit_candidate(
        self,
        *,
        market: str,
        position: Position,
        as_of_session_date: date,
        exit_reason: Literal["reversal", "trend_flip", "decommission"],
        series: BarSeries | None,
        snapshot: _IndicatorSnapshot | None = None,
        paired_entry_market: str | None = None,
    ) -> CandidateSignal:
        """Construct a ``CandidateSignal`` with ``signal_type='exit'``.

        Pricing conventions:
        - ``decision_price``: last close from the market's series when
          available. Falls back to ``position.avg_cost`` if the market has
          dropped out of ``active_universe`` (the only realistic case is
          decommission against a delisted / suspended market — the active
          universe excludes it but we still want to flatten).
        - ``stop_price``: ``Decimal('0')`` sentinel. NOT MEANINGFUL for
          exits — the dispatcher does NOT place a new bracket stop on a
          close order because the position is going to zero. The dispatcher
          must read ``signal_type`` and skip stop placement; if it ever
          treats this Decimal('0') as a real stop, that's a downstream bug
          surfacing as a $0 stop-out attempt.
        - ``direction``: ``Direction.FLAT`` sentinel = "target ending
          position is flat." The dispatcher computes the actual buy/sell
          side from a fresh ``positions_current`` read at place-order time
          (design §Q3 dispatcher-side sizing); the strategy DOES NOT
          encode +/- here.
        - ``indicators_snapshot``: populated only when a snapshot was
          computed (trend_flip). Reversal exits inherit no snapshot from
          this side — the paired entry candidate carries the indicator
          state. Decommission exits have an empty snapshot — no indicator
          participated in the decision.
        """
        decision_price = (
            series.bars[-1].close if series is not None and series.bars else position.avg_cost
        )
        indicators: dict[str, Decimal | int] = {}
        if snapshot is not None:
            indicators = {
                "ma_fast": snapshot.ma_fast,
                "ma_slow": snapshot.ma_slow,
                "last_close": snapshot.last_close,
                "atr": snapshot.atr,
            }
        return CandidateSignal(
            market=market,
            direction=Direction.FLAT,
            signal_type="exit",
            session_date=as_of_session_date,
            decision_price=decision_price,
            stop_price=Decimal("0"),
            indicators_snapshot=indicators,
            exit_reason=exit_reason,
            prior_position_direction=position.direction,
            prior_position_quantity=position.quantity,
            paired_entry_market=paired_entry_market,
        )
