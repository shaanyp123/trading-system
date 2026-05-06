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
from typing import Final


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
    DATA_QUALITY_QUARANTINE = "data_quality_quarantine"


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
    - `signal_type` — discriminator persisted to `signals.signal_type`
    - `decision_price` — close used for the breakout check (basis for slippage
      calibration a/β regression in `services/calibration/ols.py`)
    - `stop_price` — ATR-based stop (used by execution to place stop-market)
    - `indicators_snapshot` — the per-market indicator values that justified
      the signal, for inclusion in `sizing_trace.strategy_inputs` (and operator
      review in the in-app PR review surface)
    """

    market: str
    direction: Direction
    signal_type: str  # 'donchian_breakout' for V1; future strategies add their own
    session_date: date
    decision_price: Decimal
    stop_price: Decimal
    indicators_snapshot: dict[str, Decimal | int]


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


# Strategy-emitted audit event types (subset of canonical AuditEventType enum
# from backend-spec §3.30). Signal service writes these via append_audit_event.
EMITTED_EVENT_TYPES: Final[tuple[str, ...]] = (
    "signal_emitted",
    "signal_rejected",
    "universe_exclusion",
    "universe_inclusion",
)
