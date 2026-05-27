"""Exit-pipeline PR-C (2026-05-27) unit tests for the HALT_NEW gate.

Per ``Docs/exit-pipeline-design.md`` §7 + L5 + ``Docs/backend-spec.md`` §2.5:
HALT_NEW blocks new entry approvals but ALLOWS exit approvals (the
operator must still be able to flatten or rebalance during a halt).

Tests run against the pure-policy ``plan_signal_approve`` planner +
the gate logic inside ``apply_signal_dispatch``. Session factory is
mocked (no DB) — fetchone returns None, which surfaces SIGNAL_NOT_FOUND
when the gate didn't trip. The presence of SIGNAL_BLOCKED_BY_HALT (or
its absence) is the load-bearing assertion.

Mirrors the existing :class:`TestRiskStateGate` pattern in
``tests/unit/test_risk_signal_dispatch.py``.

A22 enforced (no audit_log writes from the planner). A06 enforced.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.risk.signal_dispatch import (
    DecisionDiaryEntryInput,
    SignalDispatchError,
    apply_signal_dispatch,
    plan_signal_approve,
    plan_signal_defer,
    plan_signal_reject,
)

_SIGNAL_ID = uuid4()
_ACCOUNT_ID = uuid4()
_USER_ID = "test-operator"


def _diary() -> DecisionDiaryEntryInput:
    return DecisionDiaryEntryInput(
        tag="manual_judgment",
        reasoning_text="test reasoning",
    )


def _mock_session_factory_with_row(*, status: str, signal_type: str):
    """Build a mocked session_factory whose validator SELECT returns a
    fake signals row with the given ``status`` + ``signal_type``.

    Sufficient to drive the gate logic in ``apply_signal_dispatch``
    BEFORE the audit + UPDATE steps. Audit writes are skipped because
    the test stops at the gate decision (raise or fall-through to a
    different error like SIGNAL_NOT_PENDING).
    """
    session = MagicMock()
    fetched_row = MagicMock()
    fetched_row.status = status
    fetched_row.signal_type = signal_type
    fetched = MagicMock()
    fetched.fetchone = MagicMock(return_value=fetched_row)
    session.execute = AsyncMock(return_value=fetched)

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


class TestHaltGateExitBypass:
    """Exit-pipeline L5: exit approvals bypass HALT_NEW; entries don't."""

    async def test_entry_approve_blocked_under_halt(self) -> None:
        """Pre-PR-C contract preserved: entry-signal approve raises
        SIGNAL_BLOCKED_BY_HALT under HALT_NEW."""
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
            signal_type="entry",
        )
        with pytest.raises(SignalDispatchError) as exc_info:
            await apply_signal_dispatch(
                plan,
                session_factory=None,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        assert exc_info.value.error_code == "SIGNAL_BLOCKED_BY_HALT"
        assert exc_info.value.details["signal_type"] == "entry"

    async def test_exit_approve_permitted_under_halt(self) -> None:
        """Exit-pipeline PR-C: exit-signal approve BYPASSES the
        HALT_NEW gate (L5). When the gate passes, the dispatcher
        proceeds to the validator SELECT — we drive the SELECT to
        return a 'pending' row of signal_type='exit' so the next
        step (audit write) would happen IF the gate had also
        passed; we don't drive the audit write itself."""
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
            signal_type="exit",
        )
        factory = _mock_session_factory_with_row(status="pending", signal_type="exit")
        # We expect the apply path to PASS the gate + reach the
        # validator + reach the audit step. Audit step will fail in
        # the test (we haven't stubbed the writer); we catch any
        # exception that is NOT SIGNAL_BLOCKED_BY_HALT as evidence
        # the gate didn't trip.
        try:
            await apply_signal_dispatch(
                plan,
                session_factory=factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        except SignalDispatchError as exc:
            # Any error_code other than SIGNAL_BLOCKED_BY_HALT proves
            # the gate didn't fire. The exit got past step 0.
            assert exc.error_code != "SIGNAL_BLOCKED_BY_HALT", (
                "exit signal incorrectly blocked by HALT_NEW gate"
            )
        except Exception:
            # Any other exception (audit writer not stubbed, etc.)
            # also proves the gate was bypassed. The gate raises
            # SignalDispatchError; if we got anything else we're
            # past the gate.
            pass

    async def test_exit_approve_permitted_under_normal(self) -> None:
        """NORMAL passes the gate for exits same as entries."""
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
            signal_type="exit",
        )
        factory = _mock_session_factory_with_row(status="pending", signal_type="exit")
        try:
            await apply_signal_dispatch(
                plan,
                session_factory=factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="NORMAL",
            )
        except SignalDispatchError as exc:
            assert exc.error_code != "SIGNAL_BLOCKED_BY_HALT"
        except Exception:
            pass

    async def test_entry_reject_not_gated_under_halt(self) -> None:
        """Reject path is never gated regardless of signal_type."""
        plan = plan_signal_reject(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
            signal_type="entry",
        )
        factory = _mock_session_factory_with_row(status="pending", signal_type="entry")
        try:
            await apply_signal_dispatch(
                plan,
                session_factory=factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        except SignalDispatchError as exc:
            assert exc.error_code != "SIGNAL_BLOCKED_BY_HALT"
        except Exception:
            pass

    async def test_exit_reject_not_gated_under_halt(self) -> None:
        plan = plan_signal_reject(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
            signal_type="exit",
        )
        factory = _mock_session_factory_with_row(status="pending", signal_type="exit")
        try:
            await apply_signal_dispatch(
                plan,
                session_factory=factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        except SignalDispatchError as exc:
            assert exc.error_code != "SIGNAL_BLOCKED_BY_HALT"
        except Exception:
            pass

    async def test_exit_defer_not_gated_under_halt(self) -> None:
        plan = plan_signal_defer(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
            signal_type="exit",
        )
        factory = _mock_session_factory_with_row(status="pending", signal_type="exit")
        try:
            await apply_signal_dispatch(
                plan,
                session_factory=factory,  # type: ignore[arg-type]
                env="paper",
                current_risk_state="HALT_NEW",
            )
        except SignalDispatchError as exc:
            assert exc.error_code != "SIGNAL_BLOCKED_BY_HALT"
        except Exception:
            pass


class TestPlannerSignalTypePlumbing:
    """plan_signal_* return a SignalDispatchPlan carrying signal_type."""

    def test_approve_default_signal_type_is_entry(self) -> None:
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
        )
        assert plan.signal_type == "entry"
        assert plan.audit_payload["signal_type"] == "entry"

    def test_approve_exit_signal_type(self) -> None:
        plan = plan_signal_approve(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            override_size=None,
            signal_type="exit",
        )
        assert plan.signal_type == "exit"
        assert plan.audit_payload["signal_type"] == "exit"

    def test_reject_signal_type_plumbed(self) -> None:
        plan = plan_signal_reject(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
            signal_type="exit",
        )
        assert plan.signal_type == "exit"
        assert plan.audit_payload["signal_type"] == "exit"

    def test_defer_signal_type_plumbed(self) -> None:
        plan = plan_signal_defer(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            decided_by_user_id=_USER_ID,
            diary_entry=_diary(),
            signal_type="exit",
        )
        assert plan.signal_type == "exit"
        assert plan.audit_payload["signal_type"] == "exit"
