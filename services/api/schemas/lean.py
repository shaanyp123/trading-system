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
    # treats missing/None as "no proximity data this cycle." Each item is
    # a dict per design §5; we accept dict[str, Any] for now and structurally
    # validate at the route handler when persistence lands in PR-B. PR-B
    # introduces a typed nested model + writes rows into ``signal_proximity``;
    # PR-A leaves the field shape loose so iteration on the per-market dict
    # in PR-B does not force a Pydantic-model migration here.
    market_evaluations: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Heartbeat-only: per-market proximity records for the /signals "
            "Watching view (PR-A of signal-proximity-design.md). Optional — "
            "absence is accepted so older LEAN deploys remain compatible. "
            "PR-A logs the count + ignores the payload; PR-B persists each "
            "row in ``signal_proximity``."
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
