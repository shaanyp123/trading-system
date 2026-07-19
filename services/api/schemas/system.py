"""services/api/schemas/system.py — `/api/system/*` schemas.

Backend-spec §4.1.3 endpoints:

  * ``SystemStatus`` + ``ReconciliationSummary`` — composite snapshot for
    landing-page paint (Day 15).
  * ``KillSwitchStatus`` — narrower projection used by the kill-switch tile
    (a strict subset of ``SystemStatus``; Day 15).
  * ``KillSwitchInvokeRequest`` / ``KillSwitchResumeRequest`` /
    ``KillSwitchTransitionResponse`` — request + response bodies for the two
    POST endpoints (Day 25 wired end-to-end).
  * ``RiskEnvelopeResponse`` + ``RiskParameterItem`` — read-only Phase-1 tile
    surface per frontend-spec §2.6.3 (Day 27).
  * ``AuditLogEntry`` + ``AuditLogPageResponse`` — audit explorer wire shape
    per frontend-spec §2.6.4 (Day 27).
  * ``WatchdogStatusResponse`` — REMOVED 2026-07-09 with the dedicated
    ``GET /api/system/watchdog`` route (external Nuremberg watchdog
    retired for a hosted uptime monitor; Docs/decisions-log.md
    2026-07-09). ``SystemStatus.watchdog_last_ping_utc`` stays for
    wire-compat.

The `vacation_*`, `deployments`, `agent-activity`, and `costs` endpoints
from §4.1.3 remain deferred (Phase 2 surfaces).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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

    ``watchdog_last_ping_utc``: the external watchdog was retired
    2026-07-09 (hosted uptime monitor replaced it). The field is retained
    for wire-compat and is permanently the epoch sentinel until/unless a
    future liveness writer populates ``liveness_probes``.
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


class IncidentReviewCreateRequest(BaseModel):
    """Body for ``POST /api/system/incident-reviews`` (§3.25 write-up gate).

    ``write_up_text`` minimum mirrors the DDL CHECK
    (``length(write_up_text) >= 100``): the write-up is the substantive
    gate on resuming an ``incident_review``-severity halt (backend-spec
    §2.4.3), so a token string must not satisfy it.
    """

    model_config = ConfigDict(extra="forbid")

    write_up_text: str = Field(min_length=100, max_length=10_000)


class IncidentReviewCreateResponse(BaseModel):
    """201-shape response for ``POST /api/system/incident-reviews``.

    ``id`` feeds ``KillSwitchResumeRequest.incident_review_id`` — the tile
    passes it straight into the resume mutation.
    """

    id: str
    authored_at_utc: datetime


class CashCaptureRequest(BaseModel):
    """Body for ``POST /api/system/cash-capture`` (Amendment C re-capture).

    ``reason`` is log-only context (why the operator re-captured — e.g.
    "converted 802.10 USDC->USD"); it gates nothing.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class CashCaptureResponse(BaseModel):
    """200-shape response for ``POST /api/system/cash-capture``.

    Money as Decimal-serialized strings ([A05]); None when the venue
    listed no account of that currency (rendered "—" upstream).
    """

    snapshot_date_utc: date
    captured_at_utc: datetime
    spot_usd: str | None
    cbi_usdc: str | None
    replaced: bool = Field(
        description=(
            "True when today's existing snapshot row was overwritten; "
            "False when this was the day's first capture."
        )
    )


class KillSwitchFalsePositiveRequest(BaseModel):
    """Body for ``POST /api/system/kill-switch/false-positive``
    (CONVALESCENT → NORMAL; 2026-07-11 operator adjudication amendment).

    Both fields are required and non-blank: the adjudication must carry
    the human rationale AND point at the concrete defect artifact
    (e.g. ``"PR #375"``) that justifies waiving the probation.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Operator rationale for adjudicating the halt false-positive.",
    )
    defect_reference: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Reference to the system-defect artifact that caused the halt "
            "(e.g. 'PR #375' or a decisions-log entry)."
        ),
    )


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


# ---------------------------------------------------------------------------
# Risk envelope (Day 27 — read-only tile per frontend-spec §2.6.3)
# ---------------------------------------------------------------------------


class RiskParameterItem(BaseModel):
    """One row in the read-only risk envelope tile.

    Display values come from the active ``parameter_sets`` row (keyed by the
    current ``parameter_set_hash``) once the parameters surface populates.
    Phase 0 returns the LOCKED spec defaults from backend-spec §2.4 +
    frontend-spec §2.6.3 because the ``parameters`` table is empty until the
    Week 4 Wed dispatcher seeds the first parameter set.

    ``value`` is a ``Decimal`` (serialized as string per dev-guide §3.8).
    ``unit`` distinguishes percent fractions (0.14 → "14%") from raw ratios
    (0.7 → "0.70") so the frontend formatter knows what suffix to append.
    ``cluster`` is set on the five cluster-cap rows and None for portfolio-
    level limits.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value: Decimal
    unit: Literal["percent", "ratio"]
    cluster: Literal["equity_index", "rates_bonds", "commodity", "crypto", "fx"] | None = None


class RiskEnvelopeResponse(BaseModel):
    """Backend-spec §4.1.3 ``GET /api/system/risk-envelope`` response.

    ``source`` reports where the values came from:

      * ``parameters_table`` — pulled from the active ``parameter_sets`` row.
      * ``spec_defaults``    — Phase 0 fallback because no row has been
        written yet (the §2.4 LOCKED defaults are returned verbatim).

    The frontend renders identically in either case; the field only matters
    for operator runbook diagnostics ("did the dispatcher wire up the
    parameter set yet, or am I looking at the seed defaults?").

    ``server_now`` mirrors ``SystemStatus.server_now`` so the frontend can
    anchor relative-time renderings consistently across the page.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[RiskParameterItem]
    parameter_set_hash: str | None
    parameter_set_active_since_utc: datetime | None
    source: Literal["parameters_table", "spec_defaults"]
    server_now: datetime


# ---------------------------------------------------------------------------
# Audit explorer (Day 27 — read-only table per frontend-spec §2.6.4)
# ---------------------------------------------------------------------------


class AuditLogEntry(BaseModel):
    """One row from ``audit_log`` projected for the §2.6.4 audit-explorer table.

    Hash columns are returned as lowercase hex strings (NOT base64) to match
    the existing ``deploy/audit/README.md`` operator runbook display + the
    ``verify_chain`` CLI output. Frontend renders them in a monospace font;
    operators paste into runbooks verbatim.

    ``payload_preview`` is the first 80 characters of the decoded JCS payload
    per spec §2.6.4 "preview (first 80 chars of payload reason)". Decoded as
    UTF-8 with ``errors='replace'`` so a malformed payload doesn't crash the
    response — replacement characters in the preview signal the operator to
    open the full event detail.

    Repaired-event provenance (``repaired_for_*``) is null for normal rows;
    the frontend renders an "↳ repaired @ ..." indicator when set, per spec
    §2.6.4 "Backfill provenance".
    """

    model_config = ConfigDict(extra="forbid")

    event_uuid: str
    sequence_no: int
    event_type: str
    env: Literal["paper", "live-small", "live-scale"]
    phase_at_emit: int
    source_clock_ts: datetime
    ingest_clock_ts: datetime
    prev_hash_hex: str = Field(min_length=64, max_length=64)
    record_hash_hex: str = Field(min_length=64, max_length=64)
    repaired_for_sequence_no: int | None = None
    repaired_for_event_timestamp: datetime | None = None
    payload_preview: str = Field(max_length=80)


class AuditLogPageResponse(BaseModel):
    """Paginated audit-log page per backend-spec §4.1.3 ``GET /api/system/audit``.

    Phase 0 walks the partition-pruned heap with no LIMIT-bypass; cursor
    encodes the last-seen ``sequence_no`` (monotonic + globally unique) so
    "load more" pages stay consistent across concurrent writers.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[AuditLogEntry]
    next_cursor: str | None
    has_more: bool


class HeartbeatItem(BaseModel):
    """One service's heartbeat snapshot for ``GET /api/system/heartbeats``.

    ``freshness`` collapses ``last_heartbeat_utc`` + age into one of three
    operator-readable labels:

      * ``fresh`` — last heartbeat within the service's stale threshold
      * ``stale`` — last heartbeat older than threshold (operator should
        investigate; follow-up PR fires a Discord P2 alert here)
      * ``unknown`` — no heartbeat recorded since api last restarted;
        not a signal of trouble on its own, just "first cycle pending"

    ``last_heartbeat_utc`` is the source-clock timestamp (LEAN's
    ``self.utc_time`` for cycle heartbeats), NOT the api receive time.
    ``count`` accumulates across the api's current uptime — a useful
    cross-check ("we expect ~1 LEAN cycle per weekday; count=14 over a
    month tracks").
    """

    model_config = ConfigDict(extra="forbid")

    service: Literal["lean_cycle"]
    last_heartbeat_utc: datetime | None
    received_at_utc: datetime | None
    seconds_since_last_heartbeat: int | None
    stale_threshold_s: int
    freshness: Literal["fresh", "stale", "unknown"]
    count: int


class HeartbeatsResponse(BaseModel):
    """Backend response for ``GET /api/system/heartbeats``.

    Phase 1 ships a single service (``lean_cycle``); follow-up PRs extend
    ``items`` with ``reconciliation_eod`` + ``order_placement_worker``.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[HeartbeatItem]
    server_now: datetime


__all__ = [
    "AuditLogEntry",
    "AuditLogPageResponse",
    "CashCaptureRequest",
    "CashCaptureResponse",
    "HeartbeatItem",
    "HeartbeatsResponse",
    "KillSwitchInvokeRequest",
    "KillSwitchResumeRequest",
    "KillSwitchStatus",
    "KillSwitchTransitionResponse",
    "ReconciliationSummary",
    "RiskEnvelopeResponse",
    "RiskParameterItem",
    "SystemStatus",
]
