"""services/api/schemas/capital_events.py — Pydantic request/response
models for POST /api/system/capital-event.

Mirrors the contract in ``services/risk/capital_events.py``. The route
layer translates these into a ``CapitalEventPlan`` then applies via
``apply_capital_event``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CapitalEventInvokeRequest(BaseModel):
    """``POST /api/system/capital-event`` request body.

    Per cutover plan §7 + backend-spec §3.20. ``amount_usd`` is a string
    on the wire (A05 — Decimal-as-string at the api boundary); the route
    coerces to ``Decimal`` before passing to the planner.

    ``current_session_no`` is optional — when omitted the route defaults
    to ``0`` (cutover-day bootstrap). Crypto-era note (decisions-log
    2026-07-10): the live m_capital_event session count is derived from
    the event's ``effective_at_utc`` UTC date, not from this absolute
    counter — the field persists as a forensic placeholder and the
    default ``0`` is always fine.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["deposit", "withdrawal"]
    amount_usd: str = Field(
        description=(
            "Magnitude of the capital event in USD. Positive Decimal-as-string "
            "per A05 (e.g., '25000' or '25000.00'). Route coerces to Decimal."
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Operator-readable explanation of the event. Persisted in the "
            "audit payload under `operator_reason`. 1-500 chars."
        ),
    )
    current_session_no: int = Field(
        default=0,
        ge=0,
        description=(
            "Legacy absolute session counter at event time (Phase-0 "
            "placeholder; forensic only). The live session count derives "
            "from effective_at_utc UTC dates — rely on the default 0."
        ),
    )


class CapitalEventInvokeResponse(BaseModel):
    """``POST /api/system/capital-event`` 200 response.

    Mirrors :class:`services.risk.capital_events.AppliedCapitalEvent` —
    the route returns the new capital_events row's id + both audit
    event_uuids (the second is ``None`` when threshold not met).
    """

    model_config = ConfigDict(extra="forbid")

    capital_event_id: str = Field(description="UUID of the new capital_events row.")
    event_type: Literal["deposit", "withdrawal"]
    amount_usd: str
    threshold_met: bool = Field(
        description=(
            "True when >= 5% of pre_event_equity (bootstrap-deposit always TRUE). "
            "Threshold-met events activate the 30-session capital_event mode."
        ),
    )
    post_event_equity: str = Field(description="Pre-event equity ± amount_usd; Decimal-as-string.")
    capital_event_audit_event_uuid: str = Field(
        description=("UUID of the CAPITAL_EVENT_DEPOSIT or CAPITAL_EVENT_WITHDRAWAL audit row."),
    )
    mode_started_audit_event_uuid: str | None = Field(
        default=None,
        description=(
            "UUID of the CAPITAL_EVENT_MODE_STARTED audit row when threshold met; "
            "None when threshold not met (no mode activation)."
        ),
    )


__all__ = [
    "CapitalEventInvokeRequest",
    "CapitalEventInvokeResponse",
]
