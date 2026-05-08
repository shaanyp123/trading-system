"""Unit tests for ``services/scheduler/vacation.py``.

§10.1 inventory:
- NEW entries blocked (test belongs to risk_state — vacation flag flips
  ``vacation_active``; the actual NEW-blocking happens at the risk
  dispatch layer, which is out of scope here)
- EXIT logic continues (same — exits aren't gated by vacation; test in
  dispatch layer)
- queued working orders cancelled at start (caller-side cleanup; not in
  this module's contract)
- ratification gate suspended (calendar service concern; not here)

Tests in this file cover the pure policy:
- start_vacation max 30 days enforcement (boundaries: 30 ok, 31 fails)
- start_vacation min 1 day enforcement (0 fails, -3 fails)
- end_vacation requires web session (Discord path explicitly rejected
  at the policy layer in addition to the API gate)
- end_vacation when no active vacation raises
- Audit + SSE payload shape (uses spec-canonical names: vacation_started
  / vacation_ended, NOT the implementation-guide's vacation_mode_toggled)
- ts_utc fallback uses tz-aware now (A06)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.scheduler.vacation import (
    VACATION_MAX_DAYS,
    VACATION_MIN_DAYS,
    VacationEndedBy,
    VacationError,
    plan_end_vacation,
    plan_start_vacation,
)


def _account_id() -> object:
    return uuid4()


# ---------------------------------------------------------------------------
# start_vacation — bounds
# ---------------------------------------------------------------------------


class TestStartVacationBounds:
    def test_start_vacation_zero_days_raises(self) -> None:
        with pytest.raises(VacationError, match="must be >= 1"):
            plan_start_vacation(account_id=_account_id(), days=0, operator_session_id="sess_abc")

    def test_start_vacation_negative_days_raises(self) -> None:
        with pytest.raises(VacationError, match="must be >= 1"):
            plan_start_vacation(account_id=_account_id(), days=-3, operator_session_id="sess_abc")

    def test_start_vacation_one_day_succeeds(self) -> None:
        plan = plan_start_vacation(
            account_id=_account_id(), days=VACATION_MIN_DAYS, operator_session_id="sess_abc"
        )
        assert plan.days == 1

    def test_start_vacation_at_max_succeeds(self) -> None:
        plan = plan_start_vacation(
            account_id=_account_id(), days=VACATION_MAX_DAYS, operator_session_id="sess_abc"
        )
        assert plan.days == 30
        assert plan.scheduled_end_utc - plan.started_at_utc == timedelta(days=30)

    def test_start_vacation_above_max_raises(self) -> None:
        with pytest.raises(VacationError, match="must be <= 30"):
            plan_start_vacation(
                account_id=_account_id(),
                days=VACATION_MAX_DAYS + 1,
                operator_session_id="sess_abc",
            )

    def test_start_vacation_far_above_max_raises(self) -> None:
        with pytest.raises(VacationError, match="must be <= 30"):
            plan_start_vacation(account_id=_account_id(), days=365, operator_session_id="sess_abc")


# ---------------------------------------------------------------------------
# start_vacation — payload + audit + SSE shape
# ---------------------------------------------------------------------------


class TestStartVacationPayload:
    def test_audit_event_uses_spec_canonical_name(self) -> None:
        """``vacation_started`` is in the locked taxonomy (§3.30); the
        implementation-guide's ``vacation_mode_toggled`` is NOT."""
        plan = plan_start_vacation(account_id=_account_id(), days=7, operator_session_id="sess_abc")
        assert plan.audit_event.event_type == "vacation_started"
        assert plan.audit_event.payload["days"] == 7
        assert plan.audit_event.payload["operator_session_id"] == "sess_abc"

    def test_sse_event_carries_vacation_active_true(self) -> None:
        plan = plan_start_vacation(
            account_id=_account_id(), days=14, operator_session_id="sess_abc"
        )
        assert plan.sse_event.event_type == "risk_state"
        assert plan.sse_event.data["vacation_active"] is True
        assert plan.sse_event.data["audit_event_uuid"] is None  # caller fills

    def test_started_at_default_is_tz_aware_utc(self) -> None:
        plan = plan_start_vacation(account_id=_account_id(), days=1, operator_session_id="sess_abc")
        assert plan.started_at_utc.tzinfo is not None  # A06

    def test_scheduled_end_explicit_started_at(self) -> None:
        start = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
        plan = plan_start_vacation(
            account_id=_account_id(),
            days=10,
            operator_session_id="sess_abc",
            started_at_utc=start,
        )
        assert plan.scheduled_end_utc == start + timedelta(days=10)


# ---------------------------------------------------------------------------
# end_vacation — web-only rule and active-row guard
# ---------------------------------------------------------------------------


class TestEndVacationGuards:
    def test_end_with_no_active_vacation_raises(self) -> None:
        with pytest.raises(VacationError, match="no active vacation"):
            plan_end_vacation(
                account_id=_account_id(),
                ended_by=VacationEndedBy.OPERATOR_WEB,
                operator_session_id="sess_abc",
                has_active_vacation=False,
            )

    def test_end_via_discord_command_raises(self) -> None:
        """Manual end CANNOT come from Discord — re-auth is web-only by
        construction (dev-guide §1.5)."""
        with pytest.raises(VacationError, match="cannot be ended via Discord"):
            plan_end_vacation(
                account_id=_account_id(),
                ended_by=VacationEndedBy.OPERATOR_COMMAND,
                operator_session_id="sess_abc",
                has_active_vacation=True,
            )

    def test_end_web_without_session_id_raises(self) -> None:
        with pytest.raises(VacationError, match="operator_session_id is required"):
            plan_end_vacation(
                account_id=_account_id(),
                ended_by=VacationEndedBy.OPERATOR_WEB,
                operator_session_id=None,
                has_active_vacation=True,
            )

    def test_end_web_with_session_id_succeeds(self) -> None:
        plan = plan_end_vacation(
            account_id=_account_id(),
            ended_by=VacationEndedBy.OPERATOR_WEB,
            operator_session_id="sess_abc",
            has_active_vacation=True,
        )
        assert plan.ended_by == VacationEndedBy.OPERATOR_WEB
        assert plan.audit_event.event_type == "vacation_ended"
        assert plan.audit_event.payload["operator_session_id"] == "sess_abc"

    def test_end_via_expiry_no_session_id_required(self) -> None:
        """Cron-driven natural end has no operator session; bypass the
        operator_session_id requirement."""
        plan = plan_end_vacation(
            account_id=_account_id(),
            ended_by=VacationEndedBy.EXPIRY,
            operator_session_id=None,
            has_active_vacation=True,
        )
        assert plan.ended_by == VacationEndedBy.EXPIRY
        assert plan.audit_event.event_type == "vacation_ended"
        assert "operator_session_id" not in plan.audit_event.payload


class TestEndVacationPayload:
    def test_sse_event_carries_vacation_active_false(self) -> None:
        plan = plan_end_vacation(
            account_id=_account_id(),
            ended_by=VacationEndedBy.OPERATOR_WEB,
            operator_session_id="sess_abc",
            has_active_vacation=True,
        )
        assert plan.sse_event.data["vacation_active"] is False
        assert plan.sse_event.data["vacation_until_utc"] is None
        assert plan.sse_event.data["ended_by"] == "operator_web"

    def test_ended_at_default_is_tz_aware_utc(self) -> None:
        plan = plan_end_vacation(
            account_id=_account_id(),
            ended_by=VacationEndedBy.OPERATOR_WEB,
            operator_session_id="sess_abc",
            has_active_vacation=True,
        )
        assert plan.ended_at_utc.tzinfo is not None  # A06
