"""Audit-event payload shapes for the four strategy-relevant event types.

These are TypedDict shapes — strict structural typing only. The strategy itself
does NOT call `append_audit_event`; the signal service does, after consuming
`SignalGenerationResult`. This module exists so the signal service has a typed
contract for what to write, and the strategy unit tests can assert payload
shape without depending on `services/audit/`.

The four event types referenced are part of the locked taxonomy in
`backend-spec.md §3.30 (AuditEventType enum)`:

- `signal_emitted` — emitted per CandidateSignal that passed all filters
- `signal_rejected` — emitted per market that the strategy rejected
- `universe_exclusion` — emitted when a market drops out of the active universe
  (Stage 0 filter; the strategy doesn't compute this — risk engine does — but
  the strategy module declares the payload shape so consumers stay consistent)
- `universe_inclusion` — symmetric: market re-enters the active universe

Anti-pattern reference (`Docs/claude-dev-guide.md §11`):
- A04 — DO NOT introduce a new audit event_type without updating the locked
  taxonomy. The four types above ARE in the locked taxonomy; this module just
  shapes the payloads that get written under them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict


class StrategyInputsTrace(TypedDict):
    """Sub-shape of sizing_trace.strategy_inputs — the strategy's contribution
    to the per-signal trace. Risk engine fills `stage_0_universe` ... `stage_5_lot_rounding`
    around these values.
    """

    donchian_high: str  # Decimal as string
    donchian_low: str
    ma_fast: str
    ma_slow: str
    efficiency_ratio: str  # was `hurst` pre-2026-06-02; old JSONB rows keep `hurst`
    atr: str
    stop_price: str
    lookback_days_donchian: int


class SignalEmittedPayload(TypedDict):
    """Payload for `event_type=signal_emitted` (backend-spec §2.10).

    Required fields per backend-spec §3.30 + §2.3 (composite identity tagging).
    Persisted to `audit_log.payload` JSONB; mirrors `signals` row keys for
    cross-referencing.
    """

    signal_id: str  # UUIDv7 string
    market: str
    direction: Literal["long", "short", "flat"]
    signal_type: str
    session_date: str  # ISO date YYYY-MM-DD
    emitted_at_utc: str  # ISO 8601 with tz
    decision_price: str  # Decimal as string
    target_contracts: int  # post-Stage 5 rounded; 0 if dropped
    strategy_hash: str  # full git SHA
    parameter_set_hash: str  # SHA-256 hex
    slippage_calibration_version_id: str  # UUID string
    strategy_inputs: StrategyInputsTrace


class SignalRejectedPayload(TypedDict):
    """Payload for `event_type=signal_rejected`.

    Strategy-level rejections (no breakout, trend filter failed, etc.). Sizing-
    induced rejections (sub_minimum_size) are emitted by the risk engine, not
    the strategy, and use a different shape — see `services/risk/`.
    """

    market: str
    session_date: str
    reason: str  # one of strategies.v1_trend_following.signals.RejectionReason
    strategy_hash: str
    parameter_set_hash: str
    indicators_snapshot: (
        StrategyInputsTrace | None
    )  # None if rejection was pre-indicator (e.g. INSUFFICIENT_BAR_HISTORY)


class UniverseExclusionPayload(TypedDict):
    """Payload for `event_type=universe_exclusion` (backend-spec §3.18).

    Emitted by the risk engine, not the strategy — declared here for type-sharing
    across the module set. Mirrors `universe_state.exclusion_reason` shape.
    """

    market: str
    exclusion_reason: Literal[
        "single_contract_notional_exceeds_50pct_equity",
        "data_executability_failed",
    ]
    single_contract_notional: str  # Decimal as string
    current_equity: str
    evaluated_at_utc: str


class UniverseInclusionPayload(TypedDict):
    """Payload for `event_type=universe_inclusion` (backend-spec §3.18 symmetric)."""

    market: str
    threshold_crossed_at_equity: str  # Decimal as string
    evaluated_at_utc: str


@dataclass(frozen=True, slots=True)
class StrategyAuditEvent:
    """Container that pairs an event_type with its payload for the signal service
    to feed into `services/audit/writer.append_audit_event()`.

    Yielded by `V1TrendFollowing.generate_signals()` for each emit/reject. The
    universe events are NOT yielded by the strategy — they originate in the
    risk engine's Stage 0 — but the type appears here so the signal service has
    one place to import all four shapes.
    """

    event_type: Literal[
        "signal_emitted",
        "signal_rejected",
        "universe_exclusion",
        "universe_inclusion",
    ]
    payload: (
        SignalEmittedPayload
        | SignalRejectedPayload
        | UniverseExclusionPayload
        | UniverseInclusionPayload
    )
