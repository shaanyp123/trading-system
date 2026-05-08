"""Unit tests for ``services/risk/state_machine.py`` — kill-switch policy.

Test inventory derived from backend-spec §10.1 row "services/risk/state_machine.py":
- NORMAL -> HALT_NEW for each trigger x each severity
- HALT_NEW -> CONVALESCENT routine (human resume)
- HALT_NEW -> CONVALESCENT incident_review (write-up gate)
- CONVALESCENT -> NORMAL after EXACTLY 5 sessions
- CONVALESCENT -> HALT_NEW resets counter

Plus invalid transition coverage and SSE/audit payload shape assertions.

Per anti-pattern A22, no audit events are written here — the policy is a
pure function and ``audit_events`` is asserted as data on the
``StateTransitionPlan``.
"""

from __future__ import annotations

import pytest

from services.risk.state_machine import (
    CONVALESCENT_SESSIONS_TO_NORMAL,
    TRIGGER_SEVERITY,
    HaltSeverity,
    IllegalTransitionError,
    RiskState,
    TransitionTrigger,
    plan_invoke_kill_switch,
    plan_resume_from_halt,
    plan_session_close,
)

# ---------------------------------------------------------------------------
# Trigger-to-severity mapping coverage
# ---------------------------------------------------------------------------


class TestTriggerSeverityMap:
    """The trigger -> severity map is exhaustive and immutable in production."""

    def test_every_trigger_has_a_severity(self) -> None:
        for trigger in TransitionTrigger:
            assert trigger in TRIGGER_SEVERITY

    def test_routine_triggers_classified_routine(self) -> None:
        routine = {
            TransitionTrigger.TRAILING_DD_BREACH,
            TransitionTrigger.DAILY_LOSS_BREACH,
            TransitionTrigger.SIGNAL_STORM,
            TransitionTrigger.RECON_MISMATCH,
            TransitionTrigger.BROKER_DISCONNECT_5M,
            TransitionTrigger.VOL_REGIME_Z_GT_2,
            TransitionTrigger.CORR_GT_0_85,
            TransitionTrigger.UNHANDLED_EXCEPTION,
            TransitionTrigger.CALENDAR_UNRATIFIED,
        }
        for trigger in routine:
            assert TRIGGER_SEVERITY[trigger] == HaltSeverity.ROUTINE

    def test_defensive_triggers_classified_defensive(self) -> None:
        defensive = {
            TransitionTrigger.HEARTBEAT_ENGAGEMENT_FAIL,
            TransitionTrigger.QC_OBJSTORE_STALE_10M,
            TransitionTrigger.WATCHDOG_AND_DISCORD_BOTH_FAIL,
        }
        for trigger in defensive:
            assert TRIGGER_SEVERITY[trigger] == HaltSeverity.DEFENSIVE_ENVELOPE

    def test_incident_triggers_classified_incident(self) -> None:
        incident = {
            TransitionTrigger.AUDIT_WRITE_FAIL,
            TransitionTrigger.HASH_CHAIN_BREAK,
            TransitionTrigger.DECOMMISSION_FLOOR,
        }
        for trigger in incident:
            assert TRIGGER_SEVERITY[trigger] == HaltSeverity.INCIDENT_REVIEW


# ---------------------------------------------------------------------------
# NORMAL -> HALT_NEW (each trigger x each severity)
# ---------------------------------------------------------------------------


class TestNormalToHalt:
    @pytest.mark.parametrize("trigger", list(TransitionTrigger))
    def test_normal_to_halt_each_trigger_emits_state_transition(
        self, trigger: TransitionTrigger
    ) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=trigger,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.prior_state == RiskState.NORMAL
        assert plan.new_state == RiskState.HALT_NEW
        assert plan.new_severity == TRIGGER_SEVERITY[trigger]
        # Exactly one audit event from a NORMAL start (no counter reset).
        assert len(plan.audit_events) == 1
        ev = plan.audit_events[0]
        assert ev.event_type == "state_transition_normal_to_halt"
        assert ev.payload["trigger"] == trigger.value
        assert ev.payload["new_severity"] == TRIGGER_SEVERITY[trigger].value
        assert plan.sse_event.data["state"] == "HALT_NEW"
        assert plan.sse_event.data["severity"] == TRIGGER_SEVERITY[trigger].value

    def test_normal_to_halt_resets_counter_even_when_already_zero(self) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.SIGNAL_STORM,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_convalescent_counter == 0

    def test_normal_to_halt_routine_severity_matches_taxonomy(self) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.SIGNAL_STORM,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_severity == HaltSeverity.ROUTINE

    def test_normal_to_halt_defensive_severity_matches_taxonomy(self) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.HEARTBEAT_ENGAGEMENT_FAIL,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_severity == HaltSeverity.DEFENSIVE_ENVELOPE

    def test_normal_to_halt_incident_severity_matches_taxonomy(self) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.HASH_CHAIN_BREAK,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_severity == HaltSeverity.INCIDENT_REVIEW


# ---------------------------------------------------------------------------
# CONVALESCENT -> HALT_NEW (counter resets; emits two audit events)
# ---------------------------------------------------------------------------


class TestConvalescentToHalt:
    def test_convalescent_to_halt_resets_counter(self) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.CONVALESCENT,
            current_severity=None,
            convalescent_counter=3,
            trigger=TransitionTrigger.DAILY_LOSS_BREACH,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_convalescent_counter == 0

    def test_convalescent_to_halt_emits_counter_reset_audit_event(self) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.CONVALESCENT,
            current_severity=None,
            convalescent_counter=3,
            trigger=TransitionTrigger.DAILY_LOSS_BREACH,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        # Two events: counter reset, then state transition.
        assert len(plan.audit_events) == 2
        assert plan.audit_events[0].event_type == "convalescent_counter_reset"
        assert plan.audit_events[0].payload["prior_counter"] == 3
        assert plan.audit_events[1].event_type == "state_transition_normal_to_halt"

    def test_convalescent_to_halt_no_counter_reset_event_when_counter_zero(self) -> None:
        plan = plan_invoke_kill_switch(
            current_state=RiskState.CONVALESCENT,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.SIGNAL_STORM,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        # Counter was 0 -> no separate counter_reset event needed.
        assert len(plan.audit_events) == 1
        assert plan.audit_events[0].event_type == "state_transition_normal_to_halt"


# ---------------------------------------------------------------------------
# HALT_NEW -> CONVALESCENT
# ---------------------------------------------------------------------------


class TestHaltToConvalescent:
    def test_halt_routine_to_convalescent_no_review_id_required(self) -> None:
        plan = plan_resume_from_halt(
            current_state=RiskState.HALT_NEW,
            current_severity=HaltSeverity.ROUTINE,
            operator_session_id="sess_abc123",
            incident_review_id=None,
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_state == RiskState.CONVALESCENT
        assert plan.new_severity is None  # CONVALESCENT carries no severity
        assert plan.new_convalescent_counter == 0
        assert plan.audit_events[0].event_type == "state_transition_halt_to_convalescent"

    def test_halt_defensive_to_convalescent_no_review_id_required(self) -> None:
        plan = plan_resume_from_halt(
            current_state=RiskState.HALT_NEW,
            current_severity=HaltSeverity.DEFENSIVE_ENVELOPE,
            operator_session_id="sess_abc123",
            incident_review_id=None,
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_state == RiskState.CONVALESCENT

    def test_halt_incident_review_resume_requires_incident_review_id(self) -> None:
        with pytest.raises(IllegalTransitionError, match="incident_review_id is required"):
            plan_resume_from_halt(
                current_state=RiskState.HALT_NEW,
                current_severity=HaltSeverity.INCIDENT_REVIEW,
                operator_session_id="sess_abc123",
                incident_review_id=None,
                timestamp_utc="2026-05-08T17:30:00Z",
            )

    def test_halt_incident_review_resume_with_review_id_succeeds(self) -> None:
        plan = plan_resume_from_halt(
            current_state=RiskState.HALT_NEW,
            current_severity=HaltSeverity.INCIDENT_REVIEW,
            operator_session_id="sess_abc123",
            incident_review_id="ir_2026_05_08_001",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.new_state == RiskState.CONVALESCENT
        ev = plan.audit_events[0]
        assert ev.payload["incident_review_id"] == "ir_2026_05_08_001"
        assert ev.payload["prior_severity"] == "incident_review"

    def test_resume_from_normal_state_raises_illegal_transition(self) -> None:
        with pytest.raises(IllegalTransitionError, match="resume only valid from HALT_NEW"):
            plan_resume_from_halt(
                current_state=RiskState.NORMAL,
                current_severity=None,
                operator_session_id="sess_abc123",
                incident_review_id=None,
                timestamp_utc="2026-05-08T17:30:00Z",
            )


# ---------------------------------------------------------------------------
# CONVALESCENT -> NORMAL after exactly 5 sessions
# ---------------------------------------------------------------------------


class TestConvalescentSessionCounter:
    def test_session_close_in_normal_returns_none(self) -> None:
        plan = plan_session_close(
            current_state=RiskState.NORMAL,
            convalescent_counter=0,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is None

    def test_session_close_in_halt_returns_none(self) -> None:
        plan = plan_session_close(
            current_state=RiskState.HALT_NEW,
            convalescent_counter=0,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is None

    @pytest.mark.parametrize("counter_before", [0, 1, 2, 3])
    def test_convalescent_counter_below_threshold_returns_none(self, counter_before: int) -> None:
        """Sessions 1-4 don't graduate. The caller still increments the
        counter via UPDATE; the policy returns None (no transition)."""
        plan = plan_session_close(
            current_state=RiskState.CONVALESCENT,
            convalescent_counter=counter_before,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is None

    def test_convalescent_counter_reaches_4_does_not_graduate(self) -> None:
        """Boundary: counter=3 BEFORE this close increments to 4. Still not 5."""
        plan = plan_session_close(
            current_state=RiskState.CONVALESCENT,
            convalescent_counter=3,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is None

    def test_convalescent_counter_reaches_5_graduates_to_normal(self) -> None:
        """Boundary: counter=4 BEFORE this close increments to 5 -> graduate."""
        plan = plan_session_close(
            current_state=RiskState.CONVALESCENT,
            convalescent_counter=4,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is not None
        assert plan.prior_state == RiskState.CONVALESCENT
        assert plan.new_state == RiskState.NORMAL
        assert plan.new_convalescent_counter == 0
        assert plan.audit_events[0].event_type == "state_transition_convalescent_to_normal"
        assert plan.audit_events[0].payload["convalescent_sessions_completed"] == 5
        assert plan.sse_event.data["state"] == "NORMAL"
        assert plan.sse_event.data["reason"] == "convalescent_graduated"

    def test_convalescent_constant_matches_spec(self) -> None:
        """Lock-in: backend-spec §2.4.3 specifies "5 CME sessions"."""
        assert CONVALESCENT_SESSIONS_TO_NORMAL == 5


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------


class TestIllegalTransitions:
    def test_invoke_kill_switch_from_halt_raises(self) -> None:
        """The facade should short-circuit when already halted; the policy
        layer treats HALT_NEW -> HALT_NEW as a caller bug."""
        with pytest.raises(IllegalTransitionError, match="from HALT_NEW"):
            plan_invoke_kill_switch(
                current_state=RiskState.HALT_NEW,
                current_severity=HaltSeverity.ROUTINE,
                convalescent_counter=0,
                trigger=TransitionTrigger.SIGNAL_STORM,
                triggered_by="risk_engine",
                timestamp_utc="2026-05-08T17:30:00Z",
            )

    def test_resume_from_convalescent_state_raises(self) -> None:
        with pytest.raises(IllegalTransitionError, match="resume only valid from HALT_NEW"):
            plan_resume_from_halt(
                current_state=RiskState.CONVALESCENT,
                current_severity=None,
                operator_session_id="sess_abc123",
                incident_review_id=None,
                timestamp_utc="2026-05-08T17:30:00Z",
            )


# ---------------------------------------------------------------------------
# Plan shape assertions (consistency between audit + SSE + counter)
# ---------------------------------------------------------------------------


class TestPlanShape:
    def test_normal_to_halt_sse_audit_uuid_is_none_for_caller_to_fill(self) -> None:
        """The policy can't know the audit_event_uuid; the caller fills it
        AFTER the audit write returns. Assert the contract."""
        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.SIGNAL_STORM,
            triggered_by="risk_engine",
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.sse_event.data["audit_event_uuid"] is None

    def test_resume_plan_records_prior_severity_in_audit(self) -> None:
        """The audit chain must capture WHAT we recovered FROM (prior severity);
        the new severity is None on entry to CONVALESCENT."""
        plan = plan_resume_from_halt(
            current_state=RiskState.HALT_NEW,
            current_severity=HaltSeverity.DEFENSIVE_ENVELOPE,
            operator_session_id="sess_abc123",
            incident_review_id=None,
            timestamp_utc="2026-05-08T17:30:00Z",
        )
        assert plan.audit_events[0].payload["prior_severity"] == "defensive_envelope"
        assert plan.new_severity is None
