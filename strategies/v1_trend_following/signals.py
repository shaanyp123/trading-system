"""Signal types — strategy-side surface that flows into `services/signal/`.

`CandidateSignal` is what the strategy returns; the signal service then runs
Stage 0-5 sizing (`services/risk/sizing.py`) and persists a row to the `signals`
table per backend-spec §3.3. The strategy does NOT compute `target_contracts`
or sizing trace; that belongs to the risk engine. This module's job is to define
the wire types and the strategy -> signal-service contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class RejectionReason(StrEnum):
    """Codes for `signal_rejected` events emitted by this strategy.

    These map 1:1 onto values used in the `signals.status` CHECK constraint
    (backend-spec §3.3) and the Order Rejection Taxonomy where applicable.
    Strategy-level rejections are pre-sizing; sizing-induced rejections (Stage
    5 sub-minimum-size, etc.) are emitted by the risk engine, not the strategy.
    """

    INSUFFICIENT_BAR_HISTORY = "insufficient_bar_history"
    TREND_FILTER_FAILED = "trend_filter_failed"
    HURST_BELOW_THRESHOLD = "hurst_below_threshold"
    NO_BREAKOUT = "no_breakout"
    MIN_HOLDING_DAYS_NOT_SATISFIED = "min_holding_days_not_satisfied"
    # Same-direction breakout while we already hold the position. V1 treats
    # each breakout as ONE entry; subsequent same-direction breakouts are
    # NOT additional entries (V1 has no explicit pyramiding rules — no add-on
    # thresholds in ATR units, no max-units cap). Without this gate, every
    # day a trend continues would emit another candidate that the operator
    # manually rejects in /signals; in production that becomes accidental
    # scaling. See ``strategy.py::_evaluate_market`` step 5.
    POSITION_ALREADY_SAME_DIRECTION = "position_already_same_direction"
    DATA_QUALITY_QUARANTINE = "data_quality_quarantine"

    # ------------------------------------------------------------------
    # Exit-pipeline rejection reasons (see exit-pipeline-design.md §5.1).
    # ------------------------------------------------------------------
    # Held position where ``close`` is still on the same side of ``MA_FAST``
    # as the position direction: long with close > ma_fast, or short with
    # close < ma_fast. The trend has not flipped, so no trend_flip exit.
    TREND_HOLDS = "trend_holds"
    # Held position where the trend HAS flipped (close < MA_FAST for LONG
    # or close > MA_FAST for SHORT) but held duration < MIN_HOLDING_DAYS.
    # Exit is deferred until the floor is met to avoid whipsaw exits.
    MIN_HOLDING_NOT_REACHED = "min_holding_not_reached"
    # Entry-side short-circuit when ``V1Parameters.strategy_decommissioned``
    # is True. Under decommission the entry pipeline emits no candidates;
    # the exit pipeline emits a CLOSE for every held position regardless
    # of indicator state (exit_reason='decommission'). Recording this on
    # the entry side prevents the "candidate emitted that can never fill"
    # anti-pattern (see design doc §7).
    STRATEGY_DECOMMISSIONED = "strategy_decommissioned"


@dataclass(frozen=True, slots=True)
class Bar:
    """Daily OHLCV bar.

    Storage uses `Decimal` for prices (anti-pattern A05: no float for money).
    Volume is an int per CME tick reporting; ETF volume fits in int64.
    """

    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class BarSeries:
    """Sequence of daily bars for a single market, sorted oldest -> newest.

    Invariant: `bars` are strictly increasing by `session_date` and contain no
    weekend/holiday gaps for the market's calendar (CME for futures, NYSE for
    ETFs). Gap-handling is the data layer's responsibility — by the time a
    `BarSeries` reaches the strategy it must be clean.
    """

    market: str
    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        if not self.bars:
            return  # empty series allowed; strategy will reject INSUFFICIENT_BAR_HISTORY
        prev = self.bars[0].session_date
        for b in self.bars[1:]:
            if b.session_date <= prev:
                raise ValueError(
                    f"BarSeries[{self.market}] not strictly increasing at {b.session_date}"
                )
            prev = b.session_date


@dataclass(frozen=True, slots=True)
class Position:
    """Current position snapshot used by the strategy for hold/exit decisions.

    Sourced from broker reconciliation in production (Phase 1: QC ObjectStore;
    Phase 2: ib-async + FlexQuery). Strategy is read-only on this; the risk
    engine + execution service are the writers.
    """

    market: str
    direction: Direction  # FLAT means no open position
    quantity: int  # signed: + for long, - for short
    avg_cost: Decimal
    opened_at_session_date: date | None  # None when direction == FLAT


@dataclass(frozen=True, slots=True)
class CandidateSignal:
    """Strategy output — pre-sizing.

    `services/signal/` consumes this and:
    1. Runs `services/risk/sizing.py` (Stage 0-5) to compute `target_contracts`
       + populate `sizing_trace`
    2. Persists a `signals` row + emits `signal_emitted` audit event with the
       canonical payload shape (see `audit_events.SignalEmittedPayload`)

    The strategy contributes:
    - `signal_type` — discriminator persisted to `signals.signal_type`. Widened
      from a free string to a 2-value Literal in 2026-05-26 PR-A of the exit
      pipeline (see exit-pipeline-design.md §5.2). Exits use ``"exit"``;
      entries keep ``"donchian_breakout"``. No DB migration required — the
      ``signals.signal_type`` column is free TEXT.
    - `decision_price` — close used for the breakout check (basis for slippage
      calibration a/β regression in `services/calibration/ols.py`). For an
      exit candidate it is the close that triggered the exit decision
      (last bar's close, or the position's avg_cost as a fallback when the
      market has dropped out of the active universe entirely).
    - `stop_price` — ATR-based stop (used by execution to place stop-market).
      ``Decimal("0")`` for exit candidates: the dispatcher does NOT place a
      new bracket stop on an explicit close because the position is going
      to zero.
    - `indicators_snapshot` — the per-market indicator values that justified
      the signal, for inclusion in `sizing_trace.strategy_inputs` (and operator
      review in the in-app PR review surface). For decommission exits this is
      an empty dict (no indicator state participated in the decision).

    Exit-only fields (all default ``None`` for entries):

    - `exit_reason` — which of (b) reversal, (c) trend_flip, (d) decommission
      tripped. None for entries. The dispatcher reads this for the audit
      payload.
    - `prior_position_direction`, `prior_position_quantity` — snapshot of
      ``Position`` state at signal-emit time. The dispatcher computes the
      ACTUAL close side and qty from a FRESH ``positions_current`` read at
      place-order time (see design doc §Q3 / dispatcher-side sizing); these
      fields are recorded for audit-trail comparison only.
    - `paired_entry_market` — populated only when ``exit_reason='reversal'``.
      Points at the entry-side market whose opposite-direction breakout
      triggered the reversal. The dispatcher uses this to serialize the
      paired EXIT-then-ENTRY sequence (see §Q1 + §11 R2). For an entry
      signal generated as the other half of a reversal it remains None;
      the linkage flows from exit → entry via the dispatcher's lookup, not
      the other direction.
    """

    market: str
    direction: Direction
    signal_type: Literal["donchian_breakout", "exit"]
    session_date: date
    decision_price: Decimal
    stop_price: Decimal
    indicators_snapshot: dict[str, Decimal | int]
    # Exit-only fields. All default None so existing entry-side construction
    # keeps working positionally and by kwarg. Field ordering is constrained
    # by Python dataclasses: fields with defaults must follow fields without.
    exit_reason: Literal["reversal", "trend_flip", "decommission"] | None = None
    prior_position_direction: Direction | None = None
    prior_position_quantity: int | None = None
    paired_entry_market: str | None = None


@dataclass(frozen=True, slots=True)
class SignalGenerationResult:
    """Aggregate output of `V1TrendFollowing.generate_signals()`.

    `signals` are CandidateSignals that passed all entry filters.
    `rejections` are markets where strategy rejected (with reason); the signal
    service emits `signal_rejected` audit events for these.
    `as_of_emitted_at_utc` is the wall-clock when the strategy ran (not the
    session_date — that's the close used for the breakout check).
    """

    signals: tuple[CandidateSignal, ...]
    rejections: tuple[tuple[str, RejectionReason], ...]
    as_of_emitted_at_utc: datetime
    strategy_hash_short: str = field(default="")  # populated by signal service post-call


@dataclass(frozen=True, slots=True)
class ExitGenerationResult:
    """Aggregate output of `V1TrendFollowing.generate_exit_candidates()`.

    Mirrors `SignalGenerationResult` so the LEAN wrapper + trigger_v1_cycle
    consume entries and exits with the same fan-out shape. ``signals`` are
    CandidateSignals with ``signal_type='exit'`` and a populated ``exit_reason``;
    ``rejections`` capture the per-market reasons no exit fired (e.g.
    TREND_HOLDS, MIN_HOLDING_NOT_REACHED, INSUFFICIENT_BAR_HISTORY).

    See exit-pipeline-design.md §5.3.
    """

    signals: tuple[CandidateSignal, ...]
    rejections: tuple[tuple[str, RejectionReason], ...]
    as_of_emitted_at_utc: datetime


# Strategy-emitted audit event types (subset of canonical AuditEventType enum
# from backend-spec §3.30). Signal service writes these via append_audit_event.
EMITTED_EVENT_TYPES: Final[tuple[str, ...]] = (
    "signal_emitted",
    "signal_rejected",
    "universe_exclusion",
    "universe_inclusion",
)
