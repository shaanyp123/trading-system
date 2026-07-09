"""Unit tests for ``services/risk/dispatch.py`` — kill-switch I/O orchestrator.

Day 25 (Week 7 Mon).

The dispatch layer is the I/O companion to the pure-policy
``services/risk/state_machine.py``. Unit tests here exercise the I/O
ordering + the SQL parameter shapes without touching real Postgres
(anti-pattern A22). The full end-to-end audit + state-row + chain-integrity
behavior is exercised by
``tests/integration/test_kill_switch_end_to_end.py`` against a real
postgres:16 testcontainer.

Test inventory:

  * ``TestEmptyPlanRejected`` — apply_state_transition raises ValueError
    when plan.audit_events is empty (defensive check; policy never emits
    such a plan in production but the writer's API contract requires it).
  * ``TestSingleAuditWrite`` — a plan with one PendingAuditEvent calls
    append_audit_event exactly once; the returned UUID flows through to
    AppliedStateTransition.state_transition_audit_event_uuid.
  * ``TestMultipleAuditWritesInOrder`` — a plan with two events (the
    CONVALESCENT→HALT_NEW case: counter_reset BEFORE state_transition)
    calls append_audit_event in plan order; the LAST event's UUID is the
    one returned.
  * ``TestRiskStateUpsertExecutesUpdateThenInsert`` — the UPSERT issues
    one UPDATE (flip is_current=FALSE) followed by one INSERT (with
    is_current=TRUE). The INSERT carries the LAST audit event's UUID.
  * ``TestAuditFirstStateLast`` — the order of operations is
    (1) all audit writes, then (2) the risk_state UPSERT. Backend-spec
    §2.10.1 audit-first invariant.
  * ``TestAppliedStateTransitionShape`` — return value carries the
    expected fields (state_transition_audit_event_uuid, new_state,
    new_severity).
  * ``TestModuleContract`` — public surface matches ``__all__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from services.audit.event_types import AuditEventType
from services.audit.models import AuditLogRecord
from services.risk import dispatch as dispatch_module
from services.risk.dispatch import (
    AppliedStateTransition,
    apply_state_transition,
)
from services.risk.state_machine import (
    HaltSeverity,
    PendingAuditEvent,
    PendingSSEEvent,
    RiskState,
    StateTransitionPlan,
    TransitionTrigger,
    plan_invoke_kill_switch,
    plan_resume_from_halt,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _RecordedExecute:
    """One ``session.execute(stmt, params)`` call captured for assertions."""

    sql: str
    params: dict[str, Any]


class _FakeBeginContext:
    """Async context manager mimicking ``AsyncSession.begin``."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        self._session.begin_count += 1
        return self._session

    async def __aexit__(self, *args: object) -> bool:
        self._session.commit_count += 1
        return False


class _FakeSession:
    """Stand-in for ``AsyncSession`` that records execute calls.

    Sufficient for dispatch-level tests because apply_state_transition
    only uses ``session.begin()`` + ``session.execute()``. The real
    append_audit_event behavior is monkeypatched out — the writer's own
    SERIALIZABLE + advisory_lock + retry logic is covered in
    ``tests/integration/test_audit_writer.py``.
    """

    def __init__(self) -> None:
        self.executes: list[_RecordedExecute] = []
        self.begin_count: int = 0
        self.commit_count: int = 0

    def begin(self) -> _FakeBeginContext:
        return _FakeBeginContext(self)

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = stmt.text if hasattr(stmt, "text") else str(stmt)
        self.executes.append(_RecordedExecute(sql=sql, params=params or {}))
        # Mimic the rowcount-only consumer pattern (no row-yielding SELECTs
        # in apply_state_transition).
        return AsyncMock(rowcount=1)


def _make_record(event_uuid: UUID, event_type: AuditEventType) -> AuditLogRecord:
    """Build an :class:`AuditLogRecord` for the fake writer to return."""
    return AuditLogRecord(
        sequence_no=1,
        event_uuid=event_uuid,
        event_type=event_type,
        ingest_clock_ts=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Empty-plan defensive check
# ---------------------------------------------------------------------------


class TestEmptyPlanRejected:
    @pytest.mark.asyncio
    async def test_value_error_on_empty_audit_events(self) -> None:
        # Construct a plan with empty audit_events (skips the constructor's
        # implicit promise; the dataclass doesn't validate emptiness itself).
        empty_plan = StateTransitionPlan(
            prior_state=RiskState.NORMAL,
            new_state=RiskState.HALT_NEW,
            prior_severity=None,
            new_severity=HaltSeverity.ROUTINE,
            new_convalescent_counter=0,
            reason="test",
            audit_events=(),
            sse_event=PendingSSEEvent(event_type="risk_state", data={}),
        )
        with pytest.raises(ValueError, match="audit_events is empty"):
            await apply_state_transition(
                plan=empty_plan,
                db=_FakeSession(),  # type: ignore[arg-type]
                account_id=uuid4(),
                env="paper",
                phase_at_emit=0,
            )


# ---------------------------------------------------------------------------
# Single-event invoke from NORMAL
# ---------------------------------------------------------------------------


class TestSingleAuditWrite:
    @pytest.mark.asyncio
    async def test_one_event_one_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_uuid = uuid4()
        fake_append = AsyncMock(
            return_value=_make_record(
                expected_uuid,
                AuditEventType.STATE_TRANSITION_NORMAL_TO_HALT,
            )
        )
        monkeypatch.setattr(dispatch_module, "append_audit_event", fake_append)

        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.TRAILING_DD_BREACH,
            triggered_by="operator",
            timestamp_utc="2026-05-18T12:00:00+00:00",
        )

        session = _FakeSession()
        account_id = uuid4()
        applied = await apply_state_transition(
            plan=plan,
            db=session,  # type: ignore[arg-type]
            account_id=account_id,
            env="paper",
            phase_at_emit=0,
        )

        assert fake_append.await_count == 1
        # The single call carries the canonical event_type string from the
        # plan, plus account_id + env + phase_at_emit forwarded.
        positional, kwargs = fake_append.await_args.args, fake_append.await_args.kwargs
        # First positional is the session, second is the event_type, third is the payload.
        assert positional[0] is session
        assert positional[1] == "state_transition_normal_to_halt"
        assert isinstance(positional[2], dict)
        assert kwargs["account_id"] == account_id
        assert kwargs["env"] == "paper"
        assert kwargs["phase_at_emit"] == 0

        assert applied.state_transition_audit_event_uuid == expected_uuid


# ---------------------------------------------------------------------------
# CONVALESCENT -> HALT_NEW (counter_reset BEFORE state_transition)
# ---------------------------------------------------------------------------


class TestMultipleAuditWritesInOrder:
    @pytest.mark.asyncio
    async def test_convalescent_to_halt_writes_counter_reset_before_state_transition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first_uuid = uuid4()
        second_uuid = uuid4()
        # The plan yields counter_reset FIRST, then state_transition_normal_to_halt.
        # The fake writer returns one UUID per call (records in order).
        fake_append = AsyncMock(
            side_effect=[
                _make_record(first_uuid, AuditEventType.CONVALESCENT_COUNTER_RESET),
                _make_record(
                    second_uuid,
                    AuditEventType.STATE_TRANSITION_NORMAL_TO_HALT,
                ),
            ]
        )
        monkeypatch.setattr(dispatch_module, "append_audit_event", fake_append)

        plan = plan_invoke_kill_switch(
            current_state=RiskState.CONVALESCENT,
            current_severity=None,
            convalescent_counter=3,
            trigger=TransitionTrigger.DAILY_LOSS_BREACH,
            triggered_by="operator",
            timestamp_utc="2026-05-18T12:00:00+00:00",
        )
        # Sanity-check the policy emits 2 events in the expected order.
        assert len(plan.audit_events) == 2
        assert plan.audit_events[0].event_type == "convalescent_counter_reset"
        assert plan.audit_events[1].event_type == "state_transition_normal_to_halt"

        session = _FakeSession()
        applied = await apply_state_transition(
            plan=plan,
            db=session,  # type: ignore[arg-type]
            account_id=uuid4(),
            env="paper",
            phase_at_emit=0,
        )

        # Writer is called twice — once per event, in plan order.
        assert fake_append.await_count == 2
        first_call_event_type = fake_append.await_args_list[0].args[1]
        second_call_event_type = fake_append.await_args_list[1].args[1]
        assert first_call_event_type == "convalescent_counter_reset"
        assert second_call_event_type == "state_transition_normal_to_halt"

        # The LAST event's UUID flows through to applied state.
        assert applied.state_transition_audit_event_uuid == second_uuid
        assert applied.state_transition_audit_event_uuid != first_uuid

    @pytest.mark.asyncio
    async def test_convalescent_counter_zero_emits_one_event_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When CONVALESCENT counter is 0 (just entered convalescent, no
        # sessions ticked), the planner skips counter_reset and emits ONLY
        # state_transition. Locks the policy contract.
        expected_uuid = uuid4()
        fake_append = AsyncMock(
            return_value=_make_record(
                expected_uuid,
                AuditEventType.STATE_TRANSITION_NORMAL_TO_HALT,
            )
        )
        monkeypatch.setattr(dispatch_module, "append_audit_event", fake_append)

        plan = plan_invoke_kill_switch(
            current_state=RiskState.CONVALESCENT,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.UNHANDLED_EXCEPTION,
            triggered_by="operator",
            timestamp_utc="2026-05-18T12:00:00+00:00",
        )
        assert len(plan.audit_events) == 1

        applied = await apply_state_transition(
            plan=plan,
            db=_FakeSession(),  # type: ignore[arg-type]
            account_id=uuid4(),
            env="paper",
            phase_at_emit=0,
        )
        assert fake_append.await_count == 1
        assert applied.state_transition_audit_event_uuid == expected_uuid


# ---------------------------------------------------------------------------
# risk_state UPSERT shape
# ---------------------------------------------------------------------------


class TestRiskStateUpsertExecutesUpdateThenInsert:
    @pytest.mark.asyncio
    async def test_update_flips_prior_is_current_then_insert(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_audit_uuid = uuid4()
        monkeypatch.setattr(
            dispatch_module,
            "append_audit_event",
            AsyncMock(
                return_value=_make_record(
                    expected_audit_uuid,
                    AuditEventType.STATE_TRANSITION_NORMAL_TO_HALT,
                )
            ),
        )

        plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.SIGNAL_STORM,
            triggered_by="operator",
            timestamp_utc="2026-05-18T12:00:00+00:00",
        )

        session = _FakeSession()
        account_id = uuid4()
        await apply_state_transition(
            plan=plan,
            db=session,  # type: ignore[arg-type]
            account_id=account_id,
            env="paper",
            phase_at_emit=0,
        )

        # Two executes recorded: UPDATE then INSERT.
        assert len(session.executes) == 2
        update_call = session.executes[0]
        insert_call = session.executes[1]
        assert update_call.sql.startswith("UPDATE risk_state")
        assert "is_current = FALSE" in update_call.sql
        assert update_call.params == {"acc": account_id}

        assert insert_call.sql.startswith("INSERT INTO risk_state")
        # The new row carries the state-transition audit_event_uuid.
        assert insert_call.params["acc"] == account_id
        assert insert_call.params["state"] == "HALT_NEW"
        assert insert_call.params["sev"] == "routine"
        assert insert_call.params["reason"] == "signal_storm"
        assert insert_call.params["counter"] == 0
        assert insert_call.params["audit_uuid"] == expected_audit_uuid
        # entered_at_utc is set to now() at apply-time; type-check only.
        assert isinstance(insert_call.params["entered"], datetime)
        assert insert_call.params["entered"].tzinfo is not None

        # The two writes run inside one explicit transaction.
        assert session.begin_count == 1

    @pytest.mark.asyncio
    async def test_resume_insert_uses_null_severity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # CONVALESCENT carries no severity per the §3.14 CHECK; ensure the
        # INSERT params reflect that.
        expected_audit_uuid = uuid4()
        monkeypatch.setattr(
            dispatch_module,
            "append_audit_event",
            AsyncMock(
                return_value=_make_record(
                    expected_audit_uuid,
                    AuditEventType.STATE_TRANSITION_HALT_TO_CONVALESCENT,
                )
            ),
        )

        plan = plan_resume_from_halt(
            current_state=RiskState.HALT_NEW,
            current_severity=HaltSeverity.ROUTINE,
            operator_session_id="phase0-stub-owner",
            incident_review_id=None,
            timestamp_utc="2026-05-18T12:00:00+00:00",
        )

        session = _FakeSession()
        applied = await apply_state_transition(
            plan=plan,
            db=session,  # type: ignore[arg-type]
            account_id=uuid4(),
            env="paper",
            phase_at_emit=0,
        )

        insert_call = session.executes[1]
        assert insert_call.params["state"] == "CONVALESCENT"
        assert insert_call.params["sev"] is None
        assert insert_call.params["reason"] == "human_resume"
        assert applied.new_state == "CONVALESCENT"
        assert applied.new_severity is None


# ---------------------------------------------------------------------------
# Audit-first ordering invariant
# ---------------------------------------------------------------------------


class TestAuditFirstStateLast:
    @pytest.mark.asyncio
    async def test_audit_writes_complete_before_risk_state_upsert(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The apply pipeline must finish all audit writes before touching risk_state.

        Backend-spec §2.10.1 audit-first invariant: a failure between the
        audit write and the state row leaves the audit log authoritative.
        """
        operation_order: list[str] = []

        async def recording_append(*args: Any, **kwargs: Any) -> AuditLogRecord:
            operation_order.append("audit")
            return _make_record(uuid4(), AuditEventType.STATE_TRANSITION_NORMAL_TO_HALT)

        monkeypatch.setattr(dispatch_module, "append_audit_event", recording_append)

        class _RecordingSession(_FakeSession):
            async def execute(  # type: ignore[override]
                self,
                stmt: Any,
                params: dict[str, Any] | None = None,
            ) -> Any:
                operation_order.append("state_upsert")
                return await super().execute(stmt, params)

        plan = plan_invoke_kill_switch(
            current_state=RiskState.CONVALESCENT,
            current_severity=None,
            convalescent_counter=2,  # triggers counter_reset PLUS state_transition
            trigger=TransitionTrigger.TRAILING_DD_BREACH,
            triggered_by="operator",
            timestamp_utc="2026-05-18T12:00:00+00:00",
        )
        await apply_state_transition(
            plan=plan,
            db=_RecordingSession(),  # type: ignore[arg-type]
            account_id=uuid4(),
            env="paper",
            phase_at_emit=0,
        )

        # Order: 2 audits THEN 2 state-upsert executes (UPDATE + INSERT).
        assert operation_order == ["audit", "audit", "state_upsert", "state_upsert"]


# ---------------------------------------------------------------------------
# AppliedStateTransition shape
# ---------------------------------------------------------------------------


class TestAppliedStateTransitionShape:
    def test_frozen_slots_dataclass(self) -> None:
        applied = AppliedStateTransition(
            state_transition_audit_event_uuid=uuid4(),
            new_state="HALT_NEW",
            new_severity="routine",
        )
        with pytest.raises((AttributeError, TypeError)):
            applied.new_state = "NORMAL"  # type: ignore[misc]

    def test_severity_may_be_none(self) -> None:
        AppliedStateTransition(
            state_transition_audit_event_uuid=uuid4(),
            new_state="CONVALESCENT",
            new_severity=None,
        )


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_all_exports(self) -> None:
        assert set(dispatch_module.__all__) == {
            "AppliedStateTransition",
            "apply_convalescent_clean_day_tick",
            "apply_state_transition",
        }

    def test_public_surface_callable(self) -> None:
        # Apply is async; AppliedStateTransition is a dataclass type.
        import inspect

        assert inspect.iscoroutinefunction(dispatch_module.apply_state_transition)
        assert inspect.isclass(dispatch_module.AppliedStateTransition)


# ---------------------------------------------------------------------------
# PendingAuditEvent type hint sanity (the policy module's type)
# ---------------------------------------------------------------------------


class TestPolicyTypeContract:
    """The dispatch module consumes PendingAuditEvent from state_machine —
    sanity-check the import still resolves cleanly so renames break here.
    """

    def test_pending_audit_event_is_state_machine_type(self) -> None:
        # If state_machine's type renames, mypy + this test catch it.
        event = PendingAuditEvent(
            event_type="state_transition_normal_to_halt",
            payload={},
        )
        assert event.event_type == "state_transition_normal_to_halt"
