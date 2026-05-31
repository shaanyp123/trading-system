"""services/api/schemas/lean.py — `/api/internal/lean/signals` schemas (Pivot-PR-A).

Pydantic v2 request + response models for the LEAN Local → backend POST path.

Architectural pivot 2026-05-12 (DP-025 → Option 4): replaces the pre-pivot
QC ObjectStore JSONL polling architecture (backend-spec §4.5.1 RETIRED).
LEAN Local pushes events directly to the backend via this endpoint; the
backend's `LeanAuthMiddleware` validates the shared bearer at the outer
edge of the middleware stack.

**Pivot-PR-A scope:** the heartbeat path only. LEAN's `v1_strategy.py`
emits ``lean_strategy_initialized`` at algorithm initialize and
``lean_cycle_heartbeat`` once per 17:30 ET signal cycle. The endpoint
writes a ``liveness_probes`` row (so the operator can confirm LEAN is
alive on the /system page) and returns 202 Accepted.

**Pivot-PR-D scope (future):** adds the ``signal_emitted`` event type with
full market / direction / target_contracts / decision_price / sizing_trace
payload. The dispatcher then routes the approved signal to the risk
engine + execution layer.

**PR-B (exit-pipeline) extension (2026-05-26 design lock).** A
``signal_type`` discriminator differentiates entry vs exit ``signal_emitted``
events on the same endpoint:

* ``signal_type='entry'`` (default for backwards compat with pre-PR-B LEAN
  images): requires the original PR-D field set
  (market / direction / target_contracts / decision_price / sizing_trace /
  strategy_version) and lands as ``signals.signal_type='entry'``.
* ``signal_type='exit'``: requires market / direction / exit_reason /
  prior_position_direction / prior_position_quantity / sizing_trace /
  strategy_version. target_contracts is OPTIONAL (the dispatcher computes
  the close qty from a fresh ``positions_current`` read at place-order
  time per design §Q3); decision_price is REQUIRED (the close used for the
  exit decision at signal-emit time). ``paired_entry_market`` is OPTIONAL
  and populated only when ``exit_reason='reversal'``.

See backend-spec §2.3 (Signal Engine post-pivot) + Docs/decisions-log.md
2026-05-12 entry for the full pivot rationale; see
Docs/exit-pipeline-design.md §Q2 / §6.1 for the discriminator decision.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Locked event-type enum for the `/api/internal/lean/signals` POST body.
#:
#: ``lean_strategy_initialized`` — emitted once at algorithm initialize. The
#: backend writes a `liveness_probes` row + logs the boot. No audit_log row
#: (boots are non-load-bearing; flooding the audit chain with restarts
#: dilutes signal-of-interest).
#:
#: ``lean_cycle_heartbeat`` — emitted once per scheduled cycle (17:30 ET).
#: Backend writes a `liveness_probes` row updating last-seen-at. No audit_log
#: row by default (Pivot-PR-A scope). Pivot-PR-D may flip this to write an
#: audit event once the dispatcher needs per-cycle traceability.
#:
#: ``signal_emitted`` — Pivot-PR-D scope. Emitted per approved signal at
#: 17:30 ET. Backend writes a `signal_emitted` audit row + INSERTs into the
#: `signals` table. NOT accepted by the Pivot-PR-A endpoint (returns 400
#: ``LEAN_EVENT_TYPE_NOT_WIRED``).
LeanEventType = Literal[
    "lean_strategy_initialized",
    "lean_cycle_heartbeat",
    "signal_emitted",
]


# ---------------------------------------------------------------------------
# Signal-proximity heartbeat payload (PR-B of signal-proximity-design.md)
#
# Mirrors the wire shape emitted by
# ``lean/v1_strategy.py::_serialize_market_proximity`` and the canonical
# enums in ``strategies/v1_trend_following/proximity.py``. The api
# persists each item as one row in ``signal_proximity`` via
# ``services/api/repos/signal_proximity.py``.
# ---------------------------------------------------------------------------

#: Per-gate categorical state. Matches ``proximity.GateState`` (line 50).
GateStateLiteral = Literal["pass", "close", "fail"]

#: The strategy-side ``GateStatus`` literal (proximity.py line 72) — names
#: the per-market discriminator between "evaluated normally", "warming up",
#: and "decommissioned". Pinned to the same set as the migration's CHECK
#: constraint so a future addition fails at both layers in lockstep.
GateStatusLiteral = Literal["ok", "warming_up", "decommissioned"]

#: The per-market "which gate is driving the worst state" label. Matches
#: the four values ``proximity.compute_market_proximity`` can emit
#: (proximity.py lines 91-92 + 276-281).
ClosestGateLiteral = Literal["donchian", "trend", "hurst", "history"]


# ---------------------------------------------------------------------------
# Exit-proximity heartbeat payload (PR-A of exit-proximity-design.md)
#
# Mirrors the wire shape emitted by
# ``lean/v1_strategy.py::_serialize_position_exit_proximity`` and the canonical
# enums in ``strategies/v1_trend_following/exit_proximity.py``. PR-A logs the
# count; PR-B persists each item as one row in ``exit_proximity``.
# ---------------------------------------------------------------------------

#: Per-trigger exit state. Matches ``exit_proximity.ExitState``.
ExitStateLiteral = Literal["holding", "near", "triggered"]

#: Position-level exit discriminator. Matches ``exit_proximity.GateStatus``.
ExitGateStatusLiteral = Literal["active", "warming_up", "min_holding_blocked", "decommissioned"]

#: Which exit trigger drives the overall state. Matches the ``closest_exit``
#: values ``compute_position_exit_proximity`` can return.
ClosestExitLiteral = Literal["stop", "reversal", "trend_flip", "decommission"]


class GateProximityItem(BaseModel):
    """One gate's wire-shape record.

    LEAN serializes ``GateProximity`` as ``{state, headroom, detail}`` with
    Decimal-as-string per A05. ``headroom`` is None when warming up;
    ``detail`` is an optional free-text tag (e.g., the literal
    ``"warming_up"`` returned by ``proximity._warming_up_gate``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: GateStateLiteral = Field(
        ...,
        description="Categorical gate state. One of 'pass', 'close', 'fail'.",
    )
    headroom: Decimal | None = Field(
        default=None,
        description=(
            "Numeric headroom value (Decimal-as-string on the wire per A05). "
            "None when the market is warming up."
        ),
    )
    detail: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional free-text qualifier (e.g., 'warming_up'). Short by "
            "design — the api stores it implicitly via gate_status, not a "
            "dedicated column."
        ),
    )


class MarketEvaluationItem(BaseModel):
    """One market's full proximity record on a single heartbeat.

    Wire shape: every field aligns 1:1 with the corresponding column on
    ``signal_proximity`` (with the per-gate sub-objects flattened into
    state + headroom columns at persistence time). Decimal-as-string per
    A05; absent numeric snapshot fields (last_close, etc.) survive as
    None to support the warming-up branch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(..., min_length=1, max_length=32)
    long_donchian: GateProximityItem
    short_donchian: GateProximityItem
    long_trend: GateProximityItem
    short_trend: GateProximityItem
    hurst: GateProximityItem
    last_close: Decimal | None = Field(default=None)
    overall_state: GateStateLiteral
    closest_gate: ClosestGateLiteral
    gate_status: GateStatusLiteral


class ExitTriggerItem(BaseModel):
    """One exit trigger's wire-shape record (trend_flip / stop / reversal /
    decommission).

    LEAN serializes ``ExitTriggerProximity`` as ``{state, headroom, detail}``
    with Decimal-as-string per A05. ``headroom`` is None for the binary
    triggers (reversal / decommission) and for warming-up / no-stop rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ExitStateLiteral = Field(
        ...,
        description="Categorical exit state. One of 'holding', 'near', 'triggered'.",
    )
    headroom: Decimal | None = Field(
        default=None,
        description=(
            "Numeric headroom (Decimal-as-string per A05). None for the binary "
            "triggers or when not computable (warming up / no stop on record)."
        ),
    )
    detail: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional free-text qualifier (e.g., 'warming_up', 'blocked by MIN_HOLDING (3d left)')."
        ),
    )


class PositionExitEvaluationItem(BaseModel):
    """One open position's full exit-proximity record on a single heartbeat.

    Wire shape mirrors ``exit_proximity.PositionExitProximity``. Decimal-as-
    string per A05; ``stop`` is a NULL-state record under Q1 = (B) (LEAN does
    not know the working stop — the api joins it at PR-B persist time).
    PR-A logs the count only; PR-B persists each item as one ``exit_proximity``
    row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(..., min_length=1, max_length=32)
    direction: Literal["long", "short"]
    held_days: int | None = Field(default=None)
    trend_flip: ExitTriggerItem
    stop: ExitTriggerItem
    reversal: ExitTriggerItem
    decommission: ExitTriggerItem
    last_close: Decimal | None = Field(default=None)
    stop_price: Decimal | None = Field(default=None)
    overall_state: ExitStateLiteral
    closest_exit: ClosestExitLiteral
    gate_status: ExitGateStatusLiteral


class LeanEventRequest(BaseModel):
    """POST body shape for ``POST /api/internal/lean/signals``.

    All datetime fields tz-aware UTC per dev-guide §3 / [A06].
    Decimal fields as strings per dev-guide §3.8 / [A05].

    LEAN emits this body via stdlib ``urllib.request`` (see
    ``lean/v1_strategy.py::_post_event``). Pydantic v2 strict shape; unknown
    fields → 422. Pivot-PR-D will extend this model with optional signal-
    emit fields (market / direction / target_contracts / decision_price /
    sizing_trace) once the dispatcher is wired.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: LeanEventType = Field(
        ...,
        description="Locked event-type enum. See module-level LeanEventType docstring.",
    )
    ts_utc: datetime = Field(
        ...,
        description="Source-clock timestamp (LEAN's `self.utc_time`). Must be tz-aware UTC.",
    )
    algorithm_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Identifier for the LEAN algorithm class. Phase 1: `v1_trend_following`.",
    )
    # Pivot-PR-A optional fields (carried by the heartbeat path).
    session_date_et: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="CME session date (ET wall-clock) — YYYY-MM-DD. Optional.",
    )
    equity_usd: Decimal | None = Field(
        default=None,
        description="LEAN-side `portfolio.total_portfolio_value` snapshot. Optional.",
    )
    live_mode: bool | None = Field(
        default=None,
        description="LEAN's `self.live_mode` flag (False=backtest; True=paper or live broker).",
    )
    # Worker-PR-4 signal_emitted-specific fields. Required when event_type=
    # "signal_emitted"; ignored otherwise. The route handler enforces
    # presence — kept out of a Pydantic model_validator because v2 stuffs
    # the raw ValueError into the validation-error ctx which FastAPI's
    # RequestValidationError handler can't JSON-serialize.
    market: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        description=("Market identifier (e.g., '/MES', '/ES'). Required for signal_emitted."),
    )
    direction: Literal["long", "short", "flat"] | None = Field(
        default=None,
        description=(
            "Position-direction enum. Required for signal_emitted. Exits "
            "emit ``direction='flat'`` (sentinel = target ending position is "
            "flat); the dispatcher computes the actual buy/sell side from "
            "the prior position at place-order time."
        ),
    )
    target_contracts: int | None = Field(
        default=None,
        description=(
            "Integer contract count from the V1 sizing pipeline. Required for "
            "entry signal_emitted; OPTIONAL for exits (the dispatcher computes "
            "the close qty from a fresh positions_current read at place-order "
            "time per exit-pipeline-design §Q3). May be 0 for a 'flat' direction "
            "signal."
        ),
    )
    decision_price: Decimal | None = Field(
        default=None,
        description=(
            "Mid-price at the LEAN signal cycle. Required for signal_emitted. "
            "Decimal-as-string per dev-guide §3.8 / A05."
        ),
    )
    sizing_trace: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-V1 sizing pipeline trace (gross_dollars, ATR-based stop, etc.). "
            "Required for signal_emitted. Persisted as the signals.sizing_trace "
            "JSONB column."
        ),
    )
    strategy_version: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "LEAN-side strategy version identifier — analog of QC's "
            "`qc_algorithm_version`. Required for signal_emitted. Phase 1: "
            "'v1_trend_following@<git-sha>'."
        ),
    )
    # PR-B exit-pipeline discriminator + companion fields. ``signal_type``
    # defaults to ``'entry'`` so pre-PR-B LEAN images (and any other emitter
    # that hasn't been updated) keep working unmodified. See
    # exit-pipeline-design.md §Q2 (discriminator over new audit event type)
    # and §5.2 (CandidateSignal widening).
    signal_type: Literal["entry", "exit"] = Field(
        default="entry",
        description=(
            "Discriminator persisted to ``signals.signal_type``. ``'entry'`` "
            "for the default donchian-breakout path; ``'exit'`` for an "
            "explicit close emitted by ``generate_exit_candidates``. Defaults "
            "to ``'entry'`` so pre-PR-B emitters keep working without code "
            "changes."
        ),
    )
    exit_reason: Literal["reversal", "trend_flip", "decommission"] | None = Field(
        default=None,
        description=(
            "Which exit condition tripped. REQUIRED for signal_type='exit'; "
            "REJECTED for signal_type='entry'. Mirrors the values populated "
            "by ``V1TrendFollowing.generate_exit_candidates``."
        ),
    )
    prior_position_direction: Literal["long", "short", "flat"] | None = Field(
        default=None,
        description=(
            "Snapshot of the held position's direction at signal-emit time. "
            "REQUIRED for signal_type='exit' (the dispatcher uses a fresh "
            "positions_current read at place-order time per §Q3, but the "
            "emit-time snapshot is preserved for audit-trail comparison). "
            "REJECTED for signal_type='entry'. Use 'long' or 'short' — "
            "'flat' is rejected because exits only fire against held positions."
        ),
    )
    prior_position_quantity: int | None = Field(
        default=None,
        description=(
            "Signed quantity of the held position at signal-emit time. "
            "REQUIRED for signal_type='exit'. Sign matches "
            "prior_position_direction (positive=long, negative=short, "
            "non-zero). REJECTED for signal_type='entry'."
        ),
    )
    paired_entry_market: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        description=(
            "Reversal-only — the market whose opposite-direction entry "
            "candidate triggered this exit. Set ONLY when "
            "exit_reason='reversal'. Used by the dispatcher to serialize "
            "the paired EXIT-then-ENTRY sequence (design §Q1 + §11 R2)."
        ),
    )
    # Heartbeat-extra fields — LEAN's `_post_event("lean_cycle_heartbeat",
    # extra={...})` includes these per-cycle summary counters. They were
    # previously rejected because the model is `extra="forbid"` and didn't
    # declare them; every cycle returned 422 and no heartbeat was ever
    # ingested. Schema-side declaration is the canonical fix — the route
    # handler can choose to persist or ignore them, but `extra="forbid"`
    # remains in force for any other unknown field.
    signals_emitted_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Heartbeat-only: count of signals LEAN emitted on this cycle. "
            "Sum of ``result.signals`` from V1TrendFollowing.generate_signals."
        ),
    )
    rejections_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Heartbeat-only: count of signal candidates LEAN rejected on this "
            "cycle. Sum of ``result.rejections`` from V1TrendFollowing.generate_signals."
        ),
    )
    error: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Heartbeat-only: error tag set when LEAN's per-cycle pipeline "
            "raised before signals could be emitted (e.g., 'v1_params_build_failed', "
            "'generate_signals_failed'). The full stack trace lands in the LEAN "
            "container log; this is a short tag for cross-reference."
        ),
    )
    # PR-A of Docs/signal-proximity-design.md — per-market gate-proximity
    # records attached to the heartbeat. Optional: older LEAN deploys (pre-
    # PR-A) MUST keep validating, so absence is acceptable and the API
    # treats missing/None as "no proximity data this cycle."
    #
    # PR-B (this PR) tightens the field from ``list[dict[str, Any]]`` to a
    # typed nested model so the persistence handler can rely on field
    # presence + type without re-validating in Python. The wire shape is
    # the output of ``lean/v1_strategy.py::_serialize_market_proximity``;
    # any drift between that serializer and this model causes a 422 at
    # the api boundary, which is preferable to a silently-malformed row
    # landing in ``signal_proximity``. Per the PR-A risk-review carry-
    # over watch-out: the tightened shape is structurally enforced here.
    market_evaluations: list[MarketEvaluationItem] | None = Field(
        default=None,
        description=(
            "Heartbeat-only: per-market proximity records for the /signals "
            "Watching view. Optional — absence is accepted so older LEAN "
            "deploys remain compatible. PR-A logged the count; PR-B "
            "persists each row in ``signal_proximity``."
        ),
    )
    # PR-A of Docs/exit-proximity-design.md — per-position exit-proximity
    # records attached to the heartbeat. Optional: older LEAN deploys (pre-
    # PR-A) MUST keep validating, so absence is acceptable and the api treats
    # missing/None as "no exit-proximity data this cycle." PR-A (this PR) logs
    # the count; PR-B persists each row in ``exit_proximity`` + enriches the
    # stop dimension from the latest working ``stop_market`` order (Q1 = (B)).
    # The wire shape is the output of
    # ``lean/v1_strategy.py::_serialize_position_exit_proximity``.
    position_exit_evaluations: list[PositionExitEvaluationItem] | None = Field(
        default=None,
        description=(
            "Heartbeat-only: per-position exit-proximity records for the Today "
            "'Exits' view. Optional — absence is accepted so older LEAN deploys "
            "remain compatible. PR-A logs the count; PR-B persists + enriches "
            "the stop dimension."
        ),
    )


class LeanEventAccepted(BaseModel):
    """Response body for a successful ``POST /api/internal/lean/signals``.

    Returns 202 Accepted (not 201 Created) because the endpoint may
    process the event asynchronously (Pivot-PR-D dispatcher will be
    fire-and-forget; the actual signal_emitted audit row + signals
    table row land in a background task).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    received_at_utc: datetime = Field(
        ...,
        description="Server-clock timestamp when the event was accepted.",
    )
    event_type: LeanEventType
    accepted: bool = Field(default=True)
    note: str | None = Field(
        default=None,
        max_length=256,
        description="Optional operator-visible note (e.g., 'liveness_probes upserted').",
    )
    # Worker-PR-4 signal_emitted response fields. Only populated when
    # event_type=signal_emitted; the heartbeat path leaves them None.
    signal_id: str | None = Field(
        default=None,
        description=(
            "UUID of the row inserted into the signals table. Only set when "
            "event_type=signal_emitted."
        ),
    )
    audit_event_uuid: str | None = Field(
        default=None,
        description=(
            "UUID of the matching signal_emitted row in audit_log. Only set "
            "when event_type=signal_emitted."
        ),
    )


class LeanParametersResponse(BaseModel):
    """Response body for ``GET /api/internal/lean/parameters`` (PR-C, design §12).

    Returns the active ``parameter_sets`` head row's ``parameters`` so the nightly
    LEAN cycle can honor the operator's runtime ``STRATEGY_DECOMMISSIONED`` flip
    (parameter-sets-bootstrap-design §12.3). When the table is empty, returns the
    SAFE operator-only-flag defaults (both ``"False"``); LEAN falls back to its
    ``lean.json`` values for every non-flag parameter (design §12.5 flag-only
    scope).

    All values are strings (the canonical ``to_canonical_dict()`` shape); the
    consumer coerces ``STRATEGY_DECOMMISSIONED`` via ``v1_strategy._coerce_bool_param``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    parameters: dict[str, str] = Field(
        ...,
        description=(
            "Active parameter set — UPPER_CASE keys, string values. The nightly "
            "cycle consumes STRATEGY_DECOMMISSIONED today; the full set is "
            "returned so a future fully-DB-driven follow-up needs no new endpoint."
        ),
    )
    parameter_set_hash: str | None = Field(
        default=None,
        description=(
            "Content hash of the active head row; null when the table is empty (source='defaults')."
        ),
    )
    source: Literal["db_head", "defaults"] = Field(
        ...,
        description=(
            "'db_head' when a parameter_sets head row exists; 'defaults' when the "
            "table is empty and the SAFE flag defaults are returned."
        ),
    )
    server_now: datetime = Field(
        ...,
        description="Server-clock timestamp at response build (tz-aware UTC).",
    )


class LeanPositionItem(BaseModel):
    """One held position in the ``GET /api/internal/lean/positions`` response (PR-A2).

    The nightly LEAN cycle maps each item to the strategy's ``Position``
    dataclass (``strategies/v1_trend_following/signals.py``): ``quantity`` sign
    → direction, ``avg_cost`` → cost basis, ``opened_at_session_date`` → the
    MIN_HOLDING_DAYS gate. Sourced from ``positions_current`` LEFT-joined to the
    open ``trades`` row for the open date.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(..., description="Root market symbol ('/MES' futures, bare 'TLT' ETF).")
    quantity: int = Field(
        ...,
        description="Signed contract/share count (positive = long, negative = short; never 0).",
    )
    avg_cost: str = Field(
        ...,
        description="Cost basis per contract/share, Decimal-as-string (A05).",
    )
    opened_at_session_date: str | None = Field(
        default=None,
        description=(
            "ET (America/New_York) calendar date the position opened, ISO 'YYYY-MM-DD'. "
            "Derived from the open trade's opened_at_utc, DST-aware, server-side. NULL when "
            "no open trade is recorded — the strategy then conservatively skips MIN_HOLDING."
        ),
    )


class LeanPositionsResponse(BaseModel):
    """Response body for ``GET /api/internal/lean/positions`` (PR-A2).

    The nightly LEAN cycle GETs this at cycle start (live mode only) and feeds
    ``positions`` to BOTH ``generate_signals`` (so the anti-pyramiding
    POSITION_ALREADY_SAME_DIRECTION guard works) and ``generate_exit_candidates``
    (so indicator exits fire) — fixing the empty-``self.portfolio`` dormancy
    documented in ``Docs/decisions-log.md`` 2026-05-31. ``exits_in_flight`` lists
    markets that already have a non-terminal exit signal; LEAN skips re-emitting
    an exit for those (idempotency). Read-only — no audit row, no state change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    positions: list[LeanPositionItem] = Field(
        default_factory=list,
        description="Every held (non-zero-quantity) position for the active account.",
    )
    exits_in_flight: list[str] = Field(
        default_factory=list,
        description=(
            "Markets with a non-terminal exit signal (status NOT IN "
            "rejected/cancelled/expired). LEAN suppresses exit re-emission for these."
        ),
    )
    server_now: datetime = Field(
        ...,
        description="Server-clock timestamp at response build (tz-aware UTC).",
    )
