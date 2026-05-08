"""Kill-switch state machine — pure-policy module (backend-spec §2.4.3).

Same pure-function design as ``services/risk/sizing.py``: the policy is a
synchronous, deterministic plan-then-apply function. This module returns
``StateTransitionPlan`` objects; the caller (``services/risk/dispatch.py``
when it lands) owns the I/O — UPDATE ``risk_state``, append audit event via
``services/audit/writer.append_audit_event()``, emit SSE ``risk_state`` event.

Why split policy from I/O:
- Anti-pattern A22: unit tests must NOT write audit events. Pure policy is
  trivially testable with zero mocking.
- The audit writer (``services/audit/writer.py``) and SSE emitter
  (``services/api/sse.emit_sse``) don't exist yet at Day 7. Coupling the state
  machine to those interfaces would either force me to stub them (risk of
  drift when they land) or block this PR on their delivery.
- The policy shape is what reviewers need to verify. The I/O is mechanical.

State model (matches ``risk_state`` schema in backend-spec §3.14):
    states     = NORMAL | HALT_NEW | CONVALESCENT
    severities = routine | defensive_envelope | incident_review  (HALT_NEW only)

Valid transitions (backend-spec §2.4.3 mermaid diagram):
    NORMAL       -> HALT_NEW       (any trigger; severity per trigger taxonomy)
    HALT_NEW     -> CONVALESCENT   (human resume; re-auth web-only)
    CONVALESCENT -> HALT_NEW       (any trigger; counter resets to 0)
    CONVALESCENT -> NORMAL         (after EXACTLY 5 clean CME sessions)

Invalid transitions (raise ``IllegalTransitionError``):
    NORMAL       -> CONVALESCENT       (must go through HALT_NEW)
    NORMAL       -> NORMAL             (no-op disallowed in policy layer)
    HALT_NEW     -> NORMAL             (must go through CONVALESCENT)
    HALT_NEW     -> HALT_NEW           (re-trigger is idempotent at facade
                                        level; policy layer rejects to surface
                                        any caller bug)

Severity rules:
- ``routine`` triggers: trailing-DD breach, daily-loss breach, signal storm,
  recon mismatch, broker disconnect 5m, vol-regime z>2, corr>0.85, unhandled
  exception, calendar unratified.
- ``defensive_envelope`` triggers: heartbeat engagement fail, QC ObjectStore
  >10m, watchdog+Discord both fail.
- ``incident_review`` triggers: audit write fail, hash chain break,
  decommission floor.

CONVALESCENT counter rules:
- Increments at each CME session close while in CONVALESCENT.
- Reaches 5 -> transition CONVALESCENT -> NORMAL (audit:
  ``state_transition_convalescent_to_normal``).
- Any new trigger while in CONVALESCENT -> transition CONVALESCENT -> HALT_NEW;
  emits BOTH a ``state_transition_*`` audit event AND a separate
  ``convalescent_counter_reset`` event so the chain shows the counter loss
  explicitly.

Tests in ``tests/unit/test_state_machine.py`` cover the §10.1 inventory:
- NORMAL -> HALT_NEW (each trigger x each severity)
- HALT_NEW -> CONVALESCENT (routine resume)
- HALT_NEW -> CONVALESCENT (incident_review resume requires ``incident_review_id``)
- CONVALESCENT -> NORMAL after EXACTLY 5 sessions (4 doesn't graduate; 5 does)
- CONVALESCENT -> HALT_NEW resets counter to 0
- Invalid transitions raise ``IllegalTransitionError``
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

# ---------------------------------------------------------------------------
# State + severity enums
# ---------------------------------------------------------------------------


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    HALT_NEW = "HALT_NEW"
    CONVALESCENT = "CONVALESCENT"


class HaltSeverity(StrEnum):
    ROUTINE = "routine"
    DEFENSIVE_ENVELOPE = "defensive_envelope"
    INCIDENT_REVIEW = "incident_review"


class TransitionTrigger(StrEnum):
    """Canonical triggers for kill-switch transitions (backend-spec §2.4.3).

    The trigger -> severity mapping is fixed (``TRIGGER_SEVERITY``); callers
    pass the trigger and the policy resolves severity. This prevents callers
    from incorrectly tagging a hash-chain break as ``routine``.
    """

    # routine
    TRAILING_DD_BREACH = "trailing_dd_breach"
    DAILY_LOSS_BREACH = "daily_loss_breach"
    SIGNAL_STORM = "signal_storm"
    RECON_MISMATCH = "recon_mismatch"
    BROKER_DISCONNECT_5M = "broker_disconnect_5m"
    VOL_REGIME_Z_GT_2 = "vol_regime_z_gt_2"
    CORR_GT_0_85 = "corr_gt_0_85"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    CALENDAR_UNRATIFIED = "calendar_unratified"
    # defensive_envelope
    HEARTBEAT_ENGAGEMENT_FAIL = "heartbeat_engagement_fail"
    QC_OBJSTORE_STALE_10M = "qc_objstore_stale_10m"
    WATCHDOG_AND_DISCORD_BOTH_FAIL = "watchdog_and_discord_both_fail"
    # incident_review
    AUDIT_WRITE_FAIL = "audit_write_fail"
    HASH_CHAIN_BREAK = "hash_chain_break"
    DECOMMISSION_FLOOR = "decommission_floor"


TRIGGER_SEVERITY: Final[dict[TransitionTrigger, HaltSeverity]] = {
    # routine
    TransitionTrigger.TRAILING_DD_BREACH: HaltSeverity.ROUTINE,
    TransitionTrigger.DAILY_LOSS_BREACH: HaltSeverity.ROUTINE,
    TransitionTrigger.SIGNAL_STORM: HaltSeverity.ROUTINE,
    TransitionTrigger.RECON_MISMATCH: HaltSeverity.ROUTINE,
    TransitionTrigger.BROKER_DISCONNECT_5M: HaltSeverity.ROUTINE,
    TransitionTrigger.VOL_REGIME_Z_GT_2: HaltSeverity.ROUTINE,
    TransitionTrigger.CORR_GT_0_85: HaltSeverity.ROUTINE,
    TransitionTrigger.UNHANDLED_EXCEPTION: HaltSeverity.ROUTINE,
    TransitionTrigger.CALENDAR_UNRATIFIED: HaltSeverity.ROUTINE,
    # defensive_envelope
    TransitionTrigger.HEARTBEAT_ENGAGEMENT_FAIL: HaltSeverity.DEFENSIVE_ENVELOPE,
    TransitionTrigger.QC_OBJSTORE_STALE_10M: HaltSeverity.DEFENSIVE_ENVELOPE,
    TransitionTrigger.WATCHDOG_AND_DISCORD_BOTH_FAIL: HaltSeverity.DEFENSIVE_ENVELOPE,
    # incident_review
    TransitionTrigger.AUDIT_WRITE_FAIL: HaltSeverity.INCIDENT_REVIEW,
    TransitionTrigger.HASH_CHAIN_BREAK: HaltSeverity.INCIDENT_REVIEW,
    TransitionTrigger.DECOMMISSION_FLOOR: HaltSeverity.INCIDENT_REVIEW,
}


#: CONVALESCENT graduates to NORMAL after exactly this many clean sessions
#: (backend-spec §2.4.3, "5 CME sessions without breach").
CONVALESCENT_SESSIONS_TO_NORMAL: Final[int] = 5


# ---------------------------------------------------------------------------
# Plan / event types
# ---------------------------------------------------------------------------


#: Canonical audit event types emitted by state transitions. These mirror the
#: locked taxonomy in backend-spec §3.30. Strings (not the future
#: ``AuditEventType`` enum) so this module doesn't depend on
#: ``services/audit/event_types.py`` (which lands in a later PR).
AuditEventTypeName = Literal[
    "state_transition_normal_to_halt",
    "state_transition_halt_to_convalescent",
    "state_transition_convalescent_to_normal",
    "convalescent_counter_reset",
    "kill_switch_triggered",
]


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """An audit event the caller writes via ``append_audit_event()``.

    Same shape as ``services.risk.sizing.PendingAuditEvent`` (deliberately —
    the caller's flush helper handles both modules' events identically).
    """

    event_type: AuditEventTypeName
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PendingSSEEvent:
    """An SSE event the caller emits via ``emit_sse()``.

    Sized for backend-spec §4.2.2 ``risk_state`` event shape:
    ``{state, severity, reason, audit_event_uuid}``. The
    ``audit_event_uuid`` is filled in by the caller AFTER the audit write
    returns its UUID — the policy layer can't know it.
    """

    event_type: Literal["risk_state"]
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StateTransitionPlan:
    """The full result of a state machine call.

    Caller:
      1. UPDATE ``risk_state`` SET state=new_state, severity=new_severity,
         reason=reason, convalescent_session_count=new_counter,
         entered_at_utc=now, audit_event_uuid=<from audit write>.
      2. ``append_audit_event(...)`` for each event in ``audit_events`` —
         SERIALIZABLE per dev-guide §5.1.
      3. ``emit_sse(...)`` for ``sse_event`` (after step 2 so
         audit_event_uuid is known).
    """

    prior_state: RiskState
    new_state: RiskState
    prior_severity: HaltSeverity | None
    new_severity: HaltSeverity | None
    new_convalescent_counter: int
    reason: str
    audit_events: tuple[PendingAuditEvent, ...]
    sse_event: PendingSSEEvent


class IllegalTransitionError(Exception):
    """Raised when a caller requests a state transition that the policy forbids.

    Distinct from ``SizingError``: this is a programming bug in the dispatch
    layer (e.g. trying NORMAL -> CONVALESCENT directly). Caller MUST NOT
    catch — let it propagate so the bug surfaces in CI. The fail-closed
    semantics mirror backend-spec §2.10.1's audit-write fail-closed rule.
    """


# ---------------------------------------------------------------------------
# Plan: invoke kill switch (NORMAL -> HALT_NEW or CONVALESCENT -> HALT_NEW)
# ---------------------------------------------------------------------------


def plan_invoke_kill_switch(
    *,
    current_state: RiskState,
    current_severity: HaltSeverity | None,
    convalescent_counter: int,
    trigger: TransitionTrigger,
    triggered_by: Literal["risk_engine", "agent", "operator", "watchdog"],
    timestamp_utc: str,
) -> StateTransitionPlan:
    """Plan a transition to HALT_NEW given a trigger.

    Valid prior states: NORMAL, CONVALESCENT.
    Idempotent at the FACADE layer — but the policy layer rejects HALT_NEW ->
    HALT_NEW so caller bugs surface; the facade should short-circuit before
    calling this if already halted.

    From CONVALESCENT, the counter is reset to 0 AND a separate
    ``convalescent_counter_reset`` audit event is emitted alongside the
    ``state_transition_normal_to_halt`` event so the chain reflects the
    counter-loss explicitly.
    """
    severity = TRIGGER_SEVERITY[trigger]
    reason = trigger.value

    if current_state == RiskState.HALT_NEW:
        raise IllegalTransitionError(
            f"plan_invoke_kill_switch from HALT_NEW (severity={current_severity}) "
            f"is invalid — facade should short-circuit on already-halted state"
        )
    if current_state not in (RiskState.NORMAL, RiskState.CONVALESCENT):
        raise IllegalTransitionError(f"plan_invoke_kill_switch from unknown state={current_state}")

    audit_events: list[PendingAuditEvent] = []
    if current_state == RiskState.CONVALESCENT and convalescent_counter > 0:
        audit_events.append(
            PendingAuditEvent(
                event_type="convalescent_counter_reset",
                payload={
                    "prior_counter": convalescent_counter,
                    "trigger": trigger.value,
                    "ts": timestamp_utc,
                },
            )
        )

    audit_events.append(
        PendingAuditEvent(
            event_type="state_transition_normal_to_halt",
            payload={
                "prior_state": current_state.value,
                "new_state": RiskState.HALT_NEW.value,
                "prior_severity": current_severity.value if current_severity else None,
                "new_severity": severity.value,
                "reason": reason,
                "trigger": trigger.value,
                "triggered_by": triggered_by,
                "ts": timestamp_utc,
            },
        )
    )

    sse_event = PendingSSEEvent(
        event_type="risk_state",
        data={
            "state": RiskState.HALT_NEW.value,
            "severity": severity.value,
            "reason": reason,
            "audit_event_uuid": None,  # filled by caller post-audit-write
        },
    )

    return StateTransitionPlan(
        prior_state=current_state,
        new_state=RiskState.HALT_NEW,
        prior_severity=current_severity,
        new_severity=severity,
        new_convalescent_counter=0,  # reset on entry to HALT_NEW
        reason=reason,
        audit_events=tuple(audit_events),
        sse_event=sse_event,
    )


# ---------------------------------------------------------------------------
# Plan: resume from halt (HALT_NEW -> CONVALESCENT)
# ---------------------------------------------------------------------------


def plan_resume_from_halt(
    *,
    current_state: RiskState,
    current_severity: HaltSeverity | None,
    operator_session_id: str,
    incident_review_id: str | None,
    timestamp_utc: str,
) -> StateTransitionPlan:
    """Plan HALT_NEW -> CONVALESCENT (human resume; re-auth at API layer).

    incident_review severity REQUIRES ``incident_review_id`` (the row in the
    ``incident_reviews`` table; backend-spec §3.25). Routine and
    defensive_envelope severities accept ``None``.

    Severity is preserved on the plan as ``prior_severity`` (so the audit
    event records what we recovered from); the new severity is None
    (CONVALESCENT carries no severity, per the schema CHECK in §3.14).
    """
    if current_state != RiskState.HALT_NEW:
        raise IllegalTransitionError(
            f"plan_resume_from_halt from state={current_state} is invalid; "
            f"resume only valid from HALT_NEW"
        )
    if current_severity == HaltSeverity.INCIDENT_REVIEW and not incident_review_id:
        raise IllegalTransitionError(
            "incident_review_id is required when resuming from "
            "severity=incident_review (backend-spec §2.4.3 incident write-up gate)"
        )

    payload: dict[str, Any] = {
        "prior_state": current_state.value,
        "new_state": RiskState.CONVALESCENT.value,
        "prior_severity": current_severity.value if current_severity else None,
        "operator_session_id": operator_session_id,
        "ts": timestamp_utc,
    }
    if incident_review_id is not None:
        payload["incident_review_id"] = incident_review_id

    audit_events = (
        PendingAuditEvent(event_type="state_transition_halt_to_convalescent", payload=payload),
    )
    sse_event = PendingSSEEvent(
        event_type="risk_state",
        data={
            "state": RiskState.CONVALESCENT.value,
            "severity": None,
            "reason": "human_resume",
            "audit_event_uuid": None,
        },
    )

    return StateTransitionPlan(
        prior_state=current_state,
        new_state=RiskState.CONVALESCENT,
        prior_severity=current_severity,
        new_severity=None,
        new_convalescent_counter=0,  # fresh counter on entry
        reason="human_resume",
        audit_events=audit_events,
        sse_event=sse_event,
    )


# ---------------------------------------------------------------------------
# Plan: CME session close — increment CONVALESCENT counter, maybe graduate
# ---------------------------------------------------------------------------


def plan_session_close(
    *,
    current_state: RiskState,
    convalescent_counter: int,
    timestamp_utc: str,
) -> StateTransitionPlan | None:
    """Plan the per-session-close counter update.

    - NORMAL: no-op (returns None).
    - HALT_NEW: no-op (counter doesn't tick during halt; returns None).
    - CONVALESCENT counter < 4: increment to counter+1, stay in CONVALESCENT.
      Returns a plan with audit event ``convalescent_session_close_tick`` —
      EXCEPT we don't have that audit type in the locked taxonomy, so we
      defer and return None for non-graduating ticks. The caller updates
      ``risk_state.convalescent_session_count`` directly without an audit
      write (low-noise; the graduate-to-NORMAL event captures the journey).
    - CONVALESCENT counter == 4: this close brings counter to 5 -> graduate
      to NORMAL. Returns a plan with the
      ``state_transition_convalescent_to_normal`` event.

    Returns ``None`` when no state transition occurs. The caller still
    increments the in-DB counter via a simple UPDATE.
    """
    if current_state != RiskState.CONVALESCENT:
        return None

    new_counter = convalescent_counter + 1
    if new_counter < CONVALESCENT_SESSIONS_TO_NORMAL:
        # Caller updates counter via UPDATE; no audit/SSE for this tick.
        return None

    # new_counter == 5: graduate.
    audit_events = (
        PendingAuditEvent(
            event_type="state_transition_convalescent_to_normal",
            payload={
                "prior_state": RiskState.CONVALESCENT.value,
                "new_state": RiskState.NORMAL.value,
                "convalescent_sessions_completed": new_counter,
                "ts": timestamp_utc,
            },
        ),
    )
    sse_event = PendingSSEEvent(
        event_type="risk_state",
        data={
            "state": RiskState.NORMAL.value,
            "severity": None,
            "reason": "convalescent_graduated",
            "audit_event_uuid": None,
        },
    )

    return StateTransitionPlan(
        prior_state=RiskState.CONVALESCENT,
        new_state=RiskState.NORMAL,
        prior_severity=None,
        new_severity=None,
        new_convalescent_counter=0,  # back to 0 in NORMAL
        reason="convalescent_graduated",
        audit_events=audit_events,
        sse_event=sse_event,
    )


__all__ = [
    "CONVALESCENT_SESSIONS_TO_NORMAL",
    "TRIGGER_SEVERITY",
    "AuditEventTypeName",
    "HaltSeverity",
    "IllegalTransitionError",
    "PendingAuditEvent",
    "PendingSSEEvent",
    "RiskState",
    "StateTransitionPlan",
    "TransitionTrigger",
    "plan_invoke_kill_switch",
    "plan_resume_from_halt",
    "plan_session_close",
]
