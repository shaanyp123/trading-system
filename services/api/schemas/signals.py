"""services/api/schemas/signals.py — `/api/signals*` schemas.

Backend-spec §4.1.2 ``SignalSummary`` / ``SignalListResponse`` / approve-reject-
defer request bodies + the embedded ``DecisionDiaryEntry``.

Day 15 ships the response shape; the signals table is empty in Phase 0 (no
strategy emit pipeline running yet). The list endpoint returns ``items=[]``
for now. The three POST endpoints validate their request bodies via these
models and return 501 ``SIGNAL_HANDLER_NOT_WIRED`` until the Week 4 Wed
dispatcher PR wires the real handlers (forbidden whitelist).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SignalAnomalyReason = Literal[
    "vol_regime_z_high",
    "capacity_above_alert",
    "recent_decision_diary_concern",
    "slippage_outlier_recent",
    "version_baseline_divergence",
]


SignalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "deferred",
    "expired",
    "working",
    "partially_filled",
    "filled",
    "cancelled",
    "closed",
    "stopped_out",
    "sub_minimum_size",
    "macro_window_drop",
    "market_drop_settlement_unavailable",
]


class SignalSummary(BaseModel):
    """One row of a paginated signal list.

    Decimal fields serialize as strings on the wire (dev-guide §3.8) — the
    Pydantic v2 default for ``Decimal`` is the string form already, since
    ``model_dump(mode='json')`` calls ``str()`` on Decimal values.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    market: str
    direction: Literal["long", "short", "flat"]
    # Entry-vs-exit discriminator (`signals.signal_type`). Typed ``str`` —
    # NOT a Literal — because the column is free TEXT with no DB CHECK
    # (signal_ingestion.py:99); the application is the canonical gate. The
    # production ingestion contract (``LeanEventRequest`` in schemas/lean.py)
    # only emits ``'entry'`` / ``'exit'``, so those are the values seen on
    # paper today; ``'donchian_breakout'`` is a sibling *backtest-only* value
    # (strategies/v1_trend_following) that never reaches this table. Keeping
    # ``str`` means an unanticipated value can never 422 this read endpoint
    # (which would break the whole /signals page). The frontend distinguishes
    # exits via ``signal_type == 'exit'`` (with ``direction == 'flat'`` as a
    # secondary tell).
    signal_type: str
    # For exit signals, the side of the position being closed
    # (``'long'`` / ``'short'``). Sourced from the signals row's
    # ``sizing_trace.lean_naive_sizing.prior_position_direction`` JSONB leaf
    # (the only place the local LEAN strategy persists it — it is NOT a
    # dedicated column). ``None`` for entries and for any exit row that
    # predates / omits the trace key. Lets the UI render "CLOSE · was long".
    prior_position_direction: str | None = None
    target_contracts: int
    decision_price: Decimal
    expected_fill_price: Decimal | None
    expected_slippage_bps: Decimal | None
    unsettled: bool
    anomaly_reasons: list[SignalAnomalyReason]
    status: SignalStatus
    emitted_at_utc: datetime
    expires_at_utc: datetime
    strategy_short_hash: str
    parameter_set_short_hash: str


class SignalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SignalSummary]
    next_cursor: str | None
    has_more: bool


class DecisionDiaryEntry(BaseModel):
    """Embedded body for reject/defer; the diary row writer lives in
    ``services/audit/decision_diary.py`` (validator already in place from PR
    #28). The route only validates the embedded shape today.
    """

    model_config = ConfigDict(extra="forbid")

    entry_class: Literal["signal_response", "forward_looking", "general"] = "signal_response"
    tag: Literal[
        "data_concern",
        "regime_concern",
        "size_concern",
        "manual_judgment",
        "other",
    ]
    reasoning_text: str = Field(min_length=10, max_length=2000)


class SignalApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_size: int | None = None


class SignalRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_diary_entry: DecisionDiaryEntry


class SignalDeferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_diary_entry: DecisionDiaryEntry


__all__ = [
    "DecisionDiaryEntry",
    "SignalAnomalyReason",
    "SignalApproveRequest",
    "SignalDeferRequest",
    "SignalListResponse",
    "SignalRejectRequest",
    "SignalStatus",
    "SignalSummary",
]
