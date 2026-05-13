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
            "fetch_current_risk_state",
            "plan_signal_approve",
            "plan_signal_defer",
            "plan_signal_reject",
            "RISK_STATES_PERMITTING_DISPATCH",
        ):
            assert hasattr(signal_dispatch, name)


# ---------------------------------------------------------------------------
# PR-H: Risk-state gate on apply_signal_dispatch
# ---------------------------------------------------------------------------


class TestRiskStateGate:
    """PR-H: ``apply_signal_dispatch`` must reject ``approve`` actions when
    ``current_risk_state='HALT_NEW'``. Reject + defer paths are NOT gated
    (operators need to clear pending signals during a halt)."""

    def test_locked_permit_set(self) -> None:
        from services.risk.signal_dispatch import RISK_STATES_PERMITTING_DISPATCH

        assert RISK_STATES_PERMITTING_DISPATCH == frozenset({"NORMAL", "CONVALESCENT"})

    async def test_approve_blocked_under_halt(self) -> None:
        """When current_risk_state is HALT_NEW, approve action raises
        SignalDispatchError with code SIGNAL_BLOCKED_BY_HALT BEFORE
        touching any DB / audit surface."""
        from services.risk.signal_dispatch import apply_signal_dispatch

        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
        )
        with pytest.raises(SignalDispatchError) as exc_info:
            await apply_signal_dispatch(
                plan,
                session_factory=None,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        assert exc_info.value.error_code == "SIGNAL_BLOCKED_BY_HALT"
        assert exc_info.value.details["current_risk_state"] == "HALT_NEW"

    async def test_approve_permitted_under_normal(self) -> None:
        """NORMAL passes the gate; the dispatcher progresses to step 1
        (signal SELECT). We monkeypatch the session_factory to surface
        SIGNAL_NOT_FOUND from the SELECT — proving the gate didn't trip
        AND the subsequent steps are reachable."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import apply_signal_dispatch

        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
        )

        session = MagicMock()
        fetched = MagicMock()
        fetched.fetchone = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        with pytest.raises(SignalDispatchError) as exc_info:
            await apply_signal_dispatch(
                plan,
                session_factory=_factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="NORMAL",
            )
        assert exc_info.value.error_code == "SIGNAL_NOT_FOUND"

    async def test_approve_permitted_under_convalescent(self) -> None:
        """CONVALESCENT passes the gate per backend-spec §2.5."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import apply_signal_dispatch

        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
        )
        session = MagicMock()
        fetched = MagicMock()
        fetched.fetchone = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        with pytest.raises(SignalDispatchError) as exc_info:
            await apply_signal_dispatch(
                plan,
                session_factory=_factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="CONVALESCENT",
            )
        assert exc_info.value.error_code == "SIGNAL_NOT_FOUND"

    async def test_none_risk_state_fails_open(self) -> None:
        """A None risk_state (degenerate — no current row) skips the gate
        and proceeds (fail-open)."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import apply_signal_dispatch

        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
        )
        session = MagicMock()
        fetched = MagicMock()
        fetched.fetchone = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        with pytest.raises(SignalDispatchError) as exc_info:
            await apply_signal_dispatch(
                plan,
                session_factory=_factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state=None,
            )
        assert exc_info.value.error_code == "SIGNAL_NOT_FOUND"

    async def test_reject_not_gated_under_halt(self) -> None:
        """Reject + defer paths are NEVER gated."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import apply_signal_dispatch

        plan = plan_signal_reject(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
        )
        session = MagicMock()
        fetched = MagicMock()
        fetched.fetchone = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        with pytest.raises(SignalDispatchError) as exc_info:
            await apply_signal_dispatch(
                plan,
                session_factory=_factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        assert exc_info.value.error_code == "SIGNAL_NOT_FOUND"

    async def test_defer_not_gated_under_halt(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import apply_signal_dispatch

        plan = plan_signal_defer(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
        )
        session = MagicMock()
        fetched = MagicMock()
        fetched.fetchone = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        with pytest.raises(SignalDispatchError) as exc_info:
            await apply_signal_dispatch(
                plan,
                session_factory=_factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        assert exc_info.value.error_code == "SIGNAL_NOT_FOUND"


class TestFetchCurrentRiskState:
    """PR-H: the risk-state read helper."""

    async def test_returns_state_when_row_present(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import fetch_current_risk_state

        session = MagicMock()
        fetched = MagicMock()
        row = MagicMock()
        row.state = "NORMAL"
        fetched.fetchone = MagicMock(return_value=row)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        result = await fetch_current_risk_state(_factory, account_id=_ACCOUNT_ID)  # type: ignore[arg-type]
        assert result == "NORMAL"

    async def test_returns_none_when_no_row(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import fetch_current_risk_state

        session = MagicMock()
        fetched = MagicMock()
        fetched.fetchone = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        result = await fetch_current_risk_state(_factory, account_id=_ACCOUNT_ID)  # type: ignore[arg-type]
        assert result is None

    async def test_returns_halt_new_when_state_is_halt(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from services.risk.signal_dispatch import fetch_current_risk_state

        session = MagicMock()
        fetched = MagicMock()
        row = MagicMock()
        row.state = "HALT_NEW"
        fetched.fetchone = MagicMock(return_value=row)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def _factory():
            yield session

        result = await fetch_current_risk_state(_factory, account_id=_ACCOUNT_ID)  # type: ignore[arg-type]
        assert result == "HALT_NEW"
