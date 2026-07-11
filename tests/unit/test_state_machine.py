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

from datetime import UTC, date, datetime

import pytest

from services.risk.state_machine import (
    CONVALESCENT_SESSIONS_TO_NORMAL,
    TRIGGER_SEVERITY,
    HaltSeverity,
    IllegalTransitionError,
    RiskState,
    TransitionTrigger,
    clean_day_skip_reason,
    plan_false_positive_graduation,
    plan_invoke_kill_switch,
    plan_resume_from_halt,
    plan_session_close,
    should_count_clean_day,
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
    """The graduation criterion is 3 clean UTC calendar DAYS — 2026-07-09
    operator amendment (decisions-log "C1 night one, CONVALESCENT
    amendment"), superseding both the CME-era backend-spec §2.4.3
    "5 sessions" and the C0-B4 "5 UTC days" recast. These tests pin the
    threshold at exactly 3 and the tick/reset mechanics; the daily
    00:15 UTC recon cycle is the tick source."""

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

    @pytest.mark.parametrize("counter_before", [0, 1])
    def test_convalescent_counter_below_threshold_returns_none(self, counter_before: int) -> None:
        """Days 1-2 don't graduate. The caller still increments the
        counter via UPDATE; the policy returns None (no transition)."""
        plan = plan_session_close(
            current_state=RiskState.CONVALESCENT,
            convalescent_counter=counter_before,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is None

    def test_convalescent_counter_reaches_2_does_not_graduate(self) -> None:
        """Boundary: counter=1 BEFORE this tick increments to 2. Still not 3."""
        plan = plan_session_close(
            current_state=RiskState.CONVALESCENT,
            convalescent_counter=1,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is None

    def test_convalescent_counter_reaches_3_graduates_to_normal(self) -> None:
        """Boundary: counter=2 BEFORE this tick increments to 3 -> graduate."""
        plan = plan_session_close(
            current_state=RiskState.CONVALESCENT,
            convalescent_counter=2,
            timestamp_utc="2026-05-08T22:00:00Z",
        )
        assert plan is not None
        assert plan.prior_state == RiskState.CONVALESCENT
        assert plan.new_state == RiskState.NORMAL
        assert plan.new_convalescent_counter == 0
        assert plan.audit_events[0].event_type == "state_transition_convalescent_to_normal"
        assert plan.audit_events[0].payload["convalescent_sessions_completed"] == 3
        assert plan.sse_event.data["state"] == "NORMAL"
        assert plan.sse_event.data["reason"] == "convalescent_graduated"

    def test_convalescent_constant_matches_amendment(self) -> None:
        """Lock-in: 3 clean UTC days per the 2026-07-09 operator amendment
        (decisions-log wins over backend-spec §2.4.3's CME-era "5")."""
        assert CONVALESCENT_SESSIONS_TO_NORMAL == 3


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


# ---------------------------------------------------------------------------
# Clean-day predicate (2026-07-09 CONVALESCENT amendment)
# ---------------------------------------------------------------------------


class TestCleanDayPredicate:
    """Pins the amended clean-day semantics: resume day counts, breach day
    never counts, once per day, CONVALESCENT only. The dispatch tick at
    00:15 UTC on day T evaluates counted_day = T-1."""

    COUNTED = date(2026, 7, 15)

    def _entered(self, day: date, hour: int = 14) -> datetime:
        return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)

    def test_full_day_in_convalescent_counts(self) -> None:
        assert should_count_clean_day(
            current_state=RiskState.CONVALESCENT,
            entered_at_utc=self._entered(date(2026, 7, 13)),
            last_counted_day_utc=date(2026, 7, 14),
            halt_day_utc=date(2026, 7, 13),
            counted_day_utc=self.COUNTED,
        )

    def test_resume_day_itself_counts(self) -> None:
        """Amendment: mid-day resume on the counted day still counts,
        provided the halt was a PRIOR day."""
        assert should_count_clean_day(
            current_state=RiskState.CONVALESCENT,
            entered_at_utc=self._entered(self.COUNTED, hour=14),
            last_counted_day_utc=None,
            halt_day_utc=date(2026, 7, 14),
            counted_day_utc=self.COUNTED,
        )

    def test_breach_day_never_counts_even_with_same_day_resume(self) -> None:
        """Same-UTC-day breach->halt->resume: the loss day is not clean."""
        assert (
            clean_day_skip_reason(
                current_state=RiskState.CONVALESCENT,
                entered_at_utc=self._entered(self.COUNTED, hour=20),
                last_counted_day_utc=None,
                halt_day_utc=self.COUNTED,
                counted_day_utc=self.COUNTED,
            )
            == "breach_day"
        )

    def test_stint_started_after_counted_day_rejected(self) -> None:
        assert (
            clean_day_skip_reason(
                current_state=RiskState.CONVALESCENT,
                entered_at_utc=self._entered(date(2026, 7, 16)),
                last_counted_day_utc=None,
                halt_day_utc=None,
                counted_day_utc=self.COUNTED,
            )
            == "stint_started_after_counted_day"
        )

    def test_already_counted_day_rejected(self) -> None:
        """Idempotency: a scheduler re-fire on the same day is a no-op."""
        assert (
            clean_day_skip_reason(
                current_state=RiskState.CONVALESCENT,
                entered_at_utc=self._entered(date(2026, 7, 13)),
                last_counted_day_utc=self.COUNTED,
                halt_day_utc=None,
                counted_day_utc=self.COUNTED,
            )
            == "already_counted"
        )

    @pytest.mark.parametrize("state", [RiskState.NORMAL, RiskState.HALT_NEW])
    def test_non_convalescent_states_rejected(self, state: RiskState) -> None:
        assert (
            clean_day_skip_reason(
                current_state=state,
                entered_at_utc=self._entered(date(2026, 7, 13)),
                last_counted_day_utc=None,
                halt_day_utc=None,
                counted_day_utc=self.COUNTED,
            )
            == "not_convalescent"
        )

    def test_no_prior_halt_row_skips_guard(self) -> None:
        """halt_day_utc=None (hand-seeded CONVALESCENT) -> guard skipped."""
        assert should_count_clean_day(
            current_state=RiskState.CONVALESCENT,
            entered_at_utc=self._entered(self.COUNTED),
            last_counted_day_utc=None,
            halt_day_utc=None,
            counted_day_utc=self.COUNTED,
        )

    def test_entered_at_midnight_of_counted_day_counts(self) -> None:
        """Boundary: entered exactly 00:00:00 of the counted day — the
        resume-day-counts rule includes it (<=, not <)."""
        assert should_count_clean_day(
            current_state=RiskState.CONVALESCENT,
            entered_at_utc=datetime(2026, 7, 15, 0, 0, 0, tzinfo=UTC),
            last_counted_day_utc=None,
            halt_day_utc=date(2026, 7, 14),
            counted_day_utc=self.COUNTED,
        )

    def test_naive_entered_at_raises(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            should_count_clean_day(
                current_state=RiskState.CONVALESCENT,
                entered_at_utc=datetime(2026, 7, 13, 14, 0, 0),
                last_counted_day_utc=None,
                halt_day_utc=None,
                counted_day_utc=self.COUNTED,
            )


# ---------------------------------------------------------------------------
# False-positive adjudication (2026-07-11 operator amendment)
# ---------------------------------------------------------------------------


class TestFalsePositiveGraduation:
    """CONVALESCENT -> NORMAL on operator adjudication of a defect-caused
    halt (decisions-log 2026-07-11; the phantom recon-break incident)."""

    TS = "2026-07-11T02:00:00+00:00"

    def _plan(self, **overrides: object) -> object:
        kwargs: dict[str, object] = {
            "current_state": RiskState.CONVALESCENT,
            "convalescent_counter": 0,
            "operator_reason": "recon compared asset keys vs product ids",
            "defect_reference": "PR #375",
            "operator_session_id": "op-session-1",
            "timestamp_utc": self.TS,
        }
        kwargs.update(overrides)
        return plan_false_positive_graduation(**kwargs)  # type: ignore[arg-type]

    def test_convalescent_graduates_to_normal_immediately(self) -> None:
        plan = self._plan()
        assert plan.prior_state == RiskState.CONVALESCENT
        assert plan.new_state == RiskState.NORMAL
        assert plan.new_severity is None
        assert plan.new_convalescent_counter == 0
        assert plan.reason == "false_positive_adjudicated"

    def test_audit_event_reuses_locked_graduation_type_with_cause(self) -> None:
        # [A04]: no new enum member — the locked convalescent_to_normal
        # event carries a distinguishing `cause` in the payload.
        plan = self._plan(convalescent_counter=1)
        assert len(plan.audit_events) == 1
        event = plan.audit_events[0]
        assert event.event_type == "state_transition_convalescent_to_normal"
        assert event.payload["cause"] == "false_positive_adjudicated"
        assert event.payload["convalescent_sessions_completed"] == 1
        assert event.payload["operator_reason"] == ("recon compared asset keys vs product ids")
        assert event.payload["defect_reference"] == "PR #375"
        assert event.payload["operator_session_id"] == "op-session-1"
        assert event.payload["ts"] == self.TS

    def test_sse_envelope_shape(self) -> None:
        plan = self._plan()
        assert plan.sse_event.event_type == "risk_state"
        assert plan.sse_event.data["state"] == RiskState.NORMAL.value
        assert plan.sse_event.data["severity"] is None
        assert plan.sse_event.data["reason"] == "false_positive_adjudicated"
        assert plan.sse_event.data["audit_event_uuid"] is None

    def test_from_normal_raises_illegal_transition(self) -> None:
        with pytest.raises(IllegalTransitionError, match="only valid from CONVALESCENT"):
            self._plan(current_state=RiskState.NORMAL)

    def test_from_halt_new_raises_never_shortcuts_live_halt(self) -> None:
        # The two-step shape is deliberate: resume first, adjudicate second.
        with pytest.raises(IllegalTransitionError, match="never shortcuts HALT_NEW"):
            self._plan(current_state=RiskState.HALT_NEW)

    def test_blank_reason_rejected(self) -> None:
        with pytest.raises(IllegalTransitionError, match="operator_reason is required"):
            self._plan(operator_reason="   ")

    def test_blank_defect_reference_rejected(self) -> None:
        # The waiver must always link to the fix that justified it.
        with pytest.raises(IllegalTransitionError, match="defect_reference is required"):
            self._plan(defect_reference="")
