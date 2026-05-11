"""services/api/schemas/system.py — `/api/system/*` schemas (subset).

Backend-spec §4.1.3:

  * ``SystemStatus`` + ``ReconciliationSummary`` — composite snapshot for
    landing-page paint.
  * ``KillSwitchStatus`` — narrower projection used by the kill-switch tile
    (a strict subset of ``SystemStatus``).
  * ``KillSwitchInvokeRequest`` / ``KillSwitchResumeRequest`` — request bodies
    for the two POST endpoints. These are validated today; the actual
    transition logic flows through ``services/risk/state_machine.py``
    (forbidden whitelist), wired by the Week 4 Wed dispatcher PR. Day 15 ships
    501 ``KILL_SWITCH_HANDLER_NOT_WIRED`` responses.

The `vacation_*`, `audit`, `deployments`, `agent-activity`, `costs`,
`watchdog`, and `risk-envelope` endpoints from §4.1.3 are NOT in Day 15 scope
(deferred to Week 5 Tue-Fri sessions).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.api.schemas.signals import SignalAnomalyReason  # noqa: F401  re-export

# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


class ReconciliationSummary(BaseModel):
    """Backend-spec §4.1.3 reconciliation summary block."""

    model_config = ConfigDict(extra="forbid")

    last_check_utc: datetime
    last_check_passed: bool
    open_breaks: int
    breaks_24h: int


class SystemStatus(BaseModel):
    """Composite system snapshot for the landing-page Today tile.

    ``halt_reason`` and ``halt_dwell_session_count`` are populated only when
    ``risk_state = HALT_NEW``. ``convalescent_session_count`` only when
    ``CONVALESCENT``. Phase-0 baseline returns ``risk_state = NORMAL``,
    ``severity = None``, both nullable counters at ``None``.

    ``server_now`` is the server's wall-clock at response build time; the
    frontend uses it to anchor relative-time renderings. RFC 3339 UTC ms-
    precision per §4.1.6 conventions; FastAPI default ISO serialization
    handles this.
    """

    model_config = ConfigDict(extra="forbid")

    risk_state: Literal["NORMAL", "HALT_NEW", "CONVALESCENT"]
    severity: Literal["routine", "defensive_envelope", "incident_review"] | None
    halt_reason: str | None
    halt_dwell_session_count: int | None
    convalescent_session_count: int | None
    vacation_active: bool
    vacation_until_utc: datetime | None
    watchdog_last_ping_utc: datetime
    reconciliation_summary: ReconciliationSummary
    is_session_active: bool
    server_now: datetime
    backend_version: str
    expected_frontend_version: str


class KillSwitchStatus(BaseModel):
    """Narrow projection of ``SystemStatus`` for the kill-switch tile.

    Phase 0 derives directly from ``risk_state`` table; the response includes
    the same audit_event_uuid linkage the frontend uses to deep-link into the
    `/system/audit` explorer.
    """

    model_config = ConfigDict(extra="forbid")

    risk_state: Literal["NORMAL", "HALT_NEW", "CONVALESCENT"]
    severity: Literal["routine", "defensive_envelope", "incident_review"] | None
    halt_reason: str | None
    last_transition_utc: datetime | None
    last_transition_audit_event_uuid: str | None


# ---------------------------------------------------------------------------
# Write endpoints (501-stubbed in Day 15)
# ---------------------------------------------------------------------------


class KillSwitchInvokeRequest(BaseModel):
    """Body for ``POST /api/system/kill-switch/invoke``.

    ``trigger`` mirrors the ``TransitionTrigger`` enum in
    ``services/risk/state_machine.py`` (without importing it — the route
    layer doesn't take a forbidden-whitelist dependency on the risk module
    in Day 15). Operator-initiated invocations from the web UI use
    ``trigger="manual_judgment"``; all other values are gated on automated
    callers (risk_engine / agent / watchdog) wired in Week 4 Wed dispatcher.
    """

    model_config = ConfigDict(extra="forbid")

    trigger: Literal[
        "trailing_dd_breach",
        "daily_loss_breach",
        "signal_storm",
        "recon_mismatch",
        "broker_disconnect_5m",
        "vol_regime_z_gt_2",
        "corr_gt_0_85",
        "unhandled_exception",
        "calendar_unratified",
        "heartbeat_engagement_fail",
        "qc_objstore_stale_10m",
        "watchdog_and_discord_both_fail",
        "audit_write_fail",
        "hash_chain_break",
        "decommission_floor",
        "manual_judgment",
    ]
    reason: str = Field(min_length=1, max_length=500)


class KillSwitchResumeRequest(BaseModel):
    """Body for ``POST /api/system/kill-switch/resume`` (HALT_NEW → CONVALESCENT).

    ``incident_review_id`` is REQUIRED when the current severity is
    ``incident_review``; the state-machine policy layer enforces the
    conditional requirement.
    """

    model_config = ConfigDict(extra="forbid")

    incident_review_id: str | None = None


class KillSwitchTransitionResponse(BaseModel):
    """Body for the 200 OK response to both ``invoke`` and ``resume``.

    The shape is symmetric across both endpoints — the differing semantics
    are encoded in the field values:

      * ``invoke`` always returns ``risk_state=HALT_NEW`` with non-null
        severity + halt_reason.
      * ``resume`` always returns ``risk_state=CONVALESCENT`` with
        ``severity=None`` (per schema §3.14 CHECK constraint).

    ``audit_event_uuid`` is the UUID of the ``state_transition_*`` audit
    row (NOT the optional ``convalescent_counter_reset`` precursor when
    one fires). Operators deep-link from this UUID into the audit
    explorer at ``GET /api/system/audit?event_uuid=...``.

    ``sse_sequence_no`` is the SSE multiplexer's monotonic sequence
    number for the corresponding ``risk_state`` envelope. Clients can use
    it to verify the SSE event matches the response (e.g., if the
    response arrives before the SSE event lands on the EventSource).
    """

    model_config = ConfigDict(extra="forbid")

    risk_state: Literal["NORMAL", "HALT_NEW", "CONVALESCENT"]
    severity: Literal["routine", "defensive_envelope", "incident_review"] | None
    halt_reason: str | None
    audit_event_uuid: str
    sse_sequence_no: int


__all__ = [
    "KillSwitchInvokeRequest",
    "KillSwitchResumeRequest",
    "KillSwitchStatus",
    "KillSwitchTransitionResponse",
    "ReconciliationSummary",
    "SystemStatus",
]
