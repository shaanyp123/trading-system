"""Unit tests for services.risk.signal_dispatch (Pivot-PR-D).

Pure-policy planner tests (TestPlanSignalApprove / Reject / Defer). The
I/O orchestrator (:func:`apply_signal_dispatch`) is exercised against a
real Postgres via testcontainers in tests/integration/ (deferred to a
follow-up PR; the integration tests follow the Day 28 PR-A
``test_qc_adapter_ingestion.py`` pattern).

A22 enforced (zero ``audit_log`` writes; planners are pure data).
A06 enforced (every datetime tz-aware UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.audit.event_types import AuditEventType
from services.risk.signal_dispatch import (
    DecisionDiaryEntryInput,
    SignalDispatchError,
    plan_signal_approve,
    plan_signal_defer,
    plan_signal_reject,
)

_SIGNAL_ID = uuid4()
_ACCOUNT_ID = uuid4()
_USER_ID = "phase0-stub-owner"


def _diary(
    *, tag: str = "data_concern", reasoning_text: str = "vol regime elevated"
) -> DecisionDiaryEntryInput:
    return DecisionDiaryEntryInput(tag=tag, reasoning_text=reasoning_text)


class TestPlanSignalApprove:
    def test_plan_shape(self) -> None:
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=3,
        )
        assert plan.signal_id == _SIGNAL_ID
        assert plan.account_id == _ACCOUNT_ID
        assert plan.action == "approve"
        assert plan.new_status == "approved"
        assert plan.audit_event_type == AuditEventType.SIGNAL_APPROVED
        assert plan.intent_to_place_order is True
        assert plan.diary_entry is None

    def test_audit_payload_includes_override_size(self) -> None:
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=2,
        )
        assert plan.audit_payload["override_size"] == 2
        assert plan.audit_payload["decided_by_user_id"] == _USER_ID
        assert plan.audit_payload["signal_id"] == str(_SIGNAL_ID)

    def test_override_size_none_is_allowed(self) -> None:
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
        )
        assert plan.audit_payload["override_size"] is None

    def test_decided_at_utc_defaults_to_now_tz_aware(self) -> None:
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
        )
        assert plan.decided_at_utc.tzinfo == UTC

    def test_naive_decided_at_rejected(self) -> None:
        with pytest.raises(SignalDispatchError) as exc_info:
            plan_signal_approve(
                signal_id=_SIGNAL_ID,
                account_id=_ACCOUNT_ID,
                decided_by_user_id=_USER_ID,
                override_size=None,
                decided_at_utc=datetime(2026, 5, 12, 17, 30),  # naive
            )
        assert exc_info.value.error_code == "TIMEZONE_REQUIRED"


class TestPlanSignalReject:
    def test_plan_shape(self) -> None:
        plan = plan_signal_reject(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
        )
        assert plan.action == "reject"
        assert plan.new_status == "rejected"
        assert plan.audit_event_type == AuditEventType.SIGNAL_REJECTED
        assert plan.intent_to_place_order is False
        assert plan.diary_entry is not None
        assert plan.diary_entry.tag == "data_concern"

    def test_audit_payload_includes_diary(self) -> None:
        plan = plan_signal_reject(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(tag="regime_concern", reasoning_text="FOMC tomorrow"),
        )
        diary = plan.audit_payload["diary_entry"]
        assert diary["tag"] == "regime_concern"
        assert diary["reasoning_text"] == "FOMC tomorrow"
        assert diary["entry_class"] == "signal_response"

    def test_missing_diary_raises(self) -> None:
        with pytest.raises(SignalDispatchError) as exc_info:
            plan_signal_reject(
                signal_id=_SIGNAL_ID,
                account_id=_ACCOUNT_ID,
                decided_by_user_id=_USER_ID,
                diary_entry=None,
            )
        assert exc_info.value.error_code == "DIARY_ENTRY_REQUIRED"

    def test_empty_reasoning_raises(self) -> None:
        with pytest.raises(SignalDispatchError) as exc_info:
            plan_signal_reject(
                signal_id=_SIGNAL_ID,
                account_id=_ACCOUNT_ID,
                decided_by_user_id=_USER_ID,
                diary_entry=DecisionDiaryEntryInput(tag="data_concern", reasoning_text="   "),
            )
        assert exc_info.value.error_code == "DIARY_REASONING_REQUIRED"

    def test_empty_tag_raises(self) -> None:
        with pytest.raises(SignalDispatchError) as exc_info:
            plan_signal_reject(
                signal_id=_SIGNAL_ID,
                account_id=_ACCOUNT_ID,
                decided_by_user_id=_USER_ID,
                diary_entry=DecisionDiaryEntryInput(tag="  ", reasoning_text="x"),
            )
        assert exc_info.value.error_code == "DIARY_TAG_REQUIRED"


class TestPlanSignalDefer:
    def test_plan_shape(self) -> None:
        plan = plan_signal_defer(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(tag="manual_judgment", reasoning_text="wait for fomc"),
        )
        assert plan.action == "defer"
        assert plan.new_status == "deferred"
        assert plan.audit_event_type == AuditEventType.SIGNAL_DEFERRED
        assert plan.intent_to_place_order is False

    def test_missing_diary_raises(self) -> None:
        with pytest.raises(SignalDispatchError) as exc_info:
            plan_signal_defer(
                signal_id=_SIGNAL_ID,
                account_id=_ACCOUNT_ID,
                decided_by_user_id=_USER_ID,
                diary_entry=None,
            )
        assert exc_info.value.error_code == "DIARY_ENTRY_REQUIRED"


class TestModuleContract:
    def test_public_surface(self) -> None:
        from services.risk import signal_dispatch

        for name in (
            "DecisionDiaryEntryInput",
            "SignalDispatchAction",
            "SignalDispatchError",
            "SignalDispatchPlan",
            "SignalDispatchResult",
            "apply_signal_dispatch",
            "plan_signal_approve",
            "plan_signal_defer",
            "plan_signal_reject",
        ):
            assert hasattr(signal_dispatch, name)
