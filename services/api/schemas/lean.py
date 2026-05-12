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

See backend-spec §2.3 (Signal Engine post-pivot) + Docs/decisions-log.md
2026-05-12 entry for the full design rationale.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

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
