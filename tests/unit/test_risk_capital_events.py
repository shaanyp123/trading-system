"""Unit tests for :mod:`services.risk.capital_events`.

Pure-policy tests against :func:`plan_capital_event`. The I/O orchestrator
:func:`apply_capital_event` is integration-tested in a separate file
(testcontainers needed because it touches audit_log + capital_events +
risk_state). A22 N/A here; no DB.

Coverage:

* Deposit happy path — threshold met → 2 audit events + dd_baseline_reset.
* Deposit below threshold — 1 audit event, no mode activation, no
  baseline reset.
* Withdrawal happy path — threshold met → 2 audit events, dd_baseline
  unchanged (None).
* Withdrawal below threshold — 1 audit event, no mode activation.
* Bootstrap deposit from $0 pre-equity — definitionally threshold-met +
  pct_of_pre_equity = 0 sentinel.
* Session-counter math: mode_active_until = current + 30,
  mode_vol_normalized_at = current + 5.
* Validation errors: AMOUNT_INVALID (zero/negative); negative pre-equity;
  WITHDRAWAL_EXCEEDS_EQUITY; TIMESTAMP_NAIVE.
* Audit-payload shape: Decimal-as-string (A05); tz-aware UTC ISO format
  (A06); operator_reason threaded through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from services.risk.capital_events import (
    CAPITAL_EVENT_MODE_DURATION_SESSIONS,
    CAPITAL_EVENT_THRESHOLD_PCT,
    CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS,
    CapitalEventError,
    CapitalEventPlan,
    plan_capital_event,
)


def _now() -> datetime:
    return datetime(2026, 6, 30, 14, 30, tzinfo=UTC)


class TestDepositPlan:
    def test_threshold_met_emits_two_events_and_resets_baseline(self) -> None:
        account = uuid4()
        plan = plan_capital_event(
            account_id=account,
            event_type="deposit",
            amount_usd=Decimal("5000"),
            pre_event_equity=Decimal("25000"),
            current_session_no=42,
            operator_reason="test deposit",
            now_utc=_now(),
        )
        assert isinstance(plan, CapitalEventPlan)
        assert plan.event_type == "deposit"
        assert plan.post_event_equity == Decimal("30000")
        # 5000 / 25000 = 0.2 = 20% > 5% threshold
        assert plan.pct_of_pre_equity == Decimal("0.2")
        assert plan.threshold_met is True
        # Deposits reset the baseline to post_event_equity
        assert plan.dd_baseline_reset_to == Decimal("30000")
        # Mode-activation session math
        assert plan.mode_active_until_session_no == 42 + CAPITAL_EVENT_MODE_DURATION_SESSIONS
        assert (
            plan.mode_vol_normalized_at_session_no
            == 42 + CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS
        )
        # 2 audit events: deposit + mode_started
        assert len(plan.audit_events) == 2
        assert plan.audit_events[0].event_type == "capital_event_deposit"
        assert plan.audit_events[1].event_type == "capital_event_mode_started"

    def test_threshold_not_met_emits_one_event_no_baseline_reset(self) -> None:
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="deposit",
            amount_usd=Decimal("100"),
            pre_event_equity=Decimal("25000"),
            current_session_no=10,
            operator_reason="micro top-up",
            now_utc=_now(),
        )
        # 100 / 25000 = 0.004 = 0.4% < 5%
        assert plan.threshold_met is False
        assert plan.dd_baseline_reset_to is None
        assert plan.mode_active_until_session_no is None
        assert plan.mode_vol_normalized_at_session_no is None
        assert len(plan.audit_events) == 1
        assert plan.audit_events[0].event_type == "capital_event_deposit"

    def test_exact_threshold_5_percent_is_met(self) -> None:
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="deposit",
            amount_usd=Decimal("1250"),
            pre_event_equity=Decimal("25000"),
            current_session_no=0,
            operator_reason="boundary test",
            now_utc=_now(),
        )
        # exactly 5%
        assert plan.pct_of_pre_equity == CAPITAL_EVENT_THRESHOLD_PCT
        assert plan.threshold_met is True

    def test_bootstrap_deposit_from_zero_equity(self) -> None:
        """Cutover plan §7 step 4: first deposit definitionally threshold-met."""
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="deposit",
            amount_usd=Decimal("25000"),
            pre_event_equity=Decimal("0"),
            current_session_no=0,
            operator_reason="initial live funding",
            now_utc=_now(),
        )
        assert plan.threshold_met is True
        # Bootstrap sentinel: avoid division-by-zero
        assert plan.pct_of_pre_equity == Decimal("0")
        assert plan.post_event_equity == Decimal("25000")
        # Deposit → baseline reset
        assert plan.dd_baseline_reset_to == Decimal("25000")
        # Session math from session 0
        assert plan.mode_active_until_session_no == 30
        assert plan.mode_vol_normalized_at_session_no == 5


class TestWithdrawalPlan:
    def test_threshold_met_emits_two_events_but_no_baseline_reset(self) -> None:
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="withdrawal",
            amount_usd=Decimal("5000"),
            pre_event_equity=Decimal("25000"),
            current_session_no=15,
            operator_reason="quarterly cash distribution",
            now_utc=_now(),
        )
        assert plan.event_type == "withdrawal"
        assert plan.post_event_equity == Decimal("20000")
        assert plan.threshold_met is True
        # Withdrawals do NOT reset baseline (post-event equity is LOWER)
        assert plan.dd_baseline_reset_to is None
        # Mode still activates (de-leverage post-capital-event)
        assert plan.mode_active_until_session_no == 15 + 30
        assert plan.mode_vol_normalized_at_session_no == 15 + 5
        assert len(plan.audit_events) == 2
        assert plan.audit_events[0].event_type == "capital_event_withdrawal"
        assert plan.audit_events[1].event_type == "capital_event_mode_started"

    def test_below_threshold_no_mode(self) -> None:
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="withdrawal",
            amount_usd=Decimal("50"),
            pre_event_equity=Decimal("25000"),
            current_session_no=20,
            operator_reason="micro fee withdraw",
            now_utc=_now(),
        )
        assert plan.threshold_met is False
        assert plan.dd_baseline_reset_to is None
        assert plan.mode_active_until_session_no is None
        assert len(plan.audit_events) == 1
        assert plan.audit_events[0].event_type == "capital_event_withdrawal"


class TestValidation:
    def test_zero_amount_rejected(self) -> None:
        with pytest.raises(CapitalEventError) as exc:
            plan_capital_event(
                account_id=uuid4(),
                event_type="deposit",
                amount_usd=Decimal("0"),
                pre_event_equity=Decimal("25000"),
                current_session_no=0,
                operator_reason="",
                now_utc=_now(),
            )
        assert exc.value.error_code == "AMOUNT_INVALID"

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(CapitalEventError) as exc:
            plan_capital_event(
                account_id=uuid4(),
                event_type="withdrawal",
                amount_usd=Decimal("-100"),
                pre_event_equity=Decimal("25000"),
                current_session_no=0,
                operator_reason="",
                now_utc=_now(),
            )
        assert exc.value.error_code == "AMOUNT_INVALID"

    def test_negative_pre_event_equity_rejected(self) -> None:
        with pytest.raises(CapitalEventError) as exc:
            plan_capital_event(
                account_id=uuid4(),
                event_type="deposit",
                amount_usd=Decimal("1000"),
                pre_event_equity=Decimal("-500"),
                current_session_no=0,
                operator_reason="",
                now_utc=_now(),
            )
        assert exc.value.error_code == "PRE_EVENT_EQUITY_NEGATIVE"

    def test_withdrawal_exceeds_equity_rejected(self) -> None:
        with pytest.raises(CapitalEventError) as exc:
            plan_capital_event(
                account_id=uuid4(),
                event_type="withdrawal",
                amount_usd=Decimal("30000"),
                pre_event_equity=Decimal("25000"),
                current_session_no=0,
                operator_reason="",
                now_utc=_now(),
            )
        assert exc.value.error_code == "WITHDRAWAL_EXCEEDS_EQUITY"

    def test_withdrawal_equal_to_equity_accepted(self) -> None:
        """Exact-balance withdrawal is allowed (post = 0; not negative)."""
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="withdrawal",
            amount_usd=Decimal("25000"),
            pre_event_equity=Decimal("25000"),
            current_session_no=0,
            operator_reason="full close",
            now_utc=_now(),
        )
        assert plan.post_event_equity == Decimal("0")

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(CapitalEventError) as exc:
            plan_capital_event(
                account_id=uuid4(),
                event_type="deposit",
                amount_usd=Decimal("100"),
                pre_event_equity=Decimal("25000"),
                current_session_no=0,
                operator_reason="",
                now_utc=datetime(2026, 6, 30, 14, 30),  # naive
            )
        assert exc.value.error_code == "TIMESTAMP_NAIVE"


class TestAuditPayloadShape:
    def test_decimal_as_string_payload(self) -> None:
        """A05: every Decimal in the payload is serialized as string."""
        account = uuid4()
        plan = plan_capital_event(
            account_id=account,
            event_type="deposit",
            amount_usd=Decimal("1000"),
            pre_event_equity=Decimal("25000"),
            current_session_no=5,
            operator_reason="test",
            now_utc=_now(),
        )
        payload = plan.audit_events[0].payload
        assert payload["amount_usd"] == "1000"
        assert payload["pre_event_equity"] == "25000"
        assert payload["post_event_equity"] == "26000"
        assert payload["pct_of_pre_equity"] == "0.04"  # 1000/25000
        # Threshold NOT met at 4% → dd_baseline_reset_to is None
        assert payload["dd_baseline_reset_to"] is None
        assert payload["account_id"] == str(account)

    def test_iso_timestamp_payload(self) -> None:
        """A06: tz-aware UTC ISO format."""
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="deposit",
            amount_usd=Decimal("5000"),
            pre_event_equity=Decimal("25000"),
            current_session_no=0,
            operator_reason="x",
            now_utc=_now(),
        )
        payload = plan.audit_events[0].payload
        assert payload["effective_at_utc"] == "2026-06-30T14:30:00+00:00"

    def test_operator_reason_threaded_through(self) -> None:
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="deposit",
            amount_usd=Decimal("5000"),
            pre_event_equity=Decimal("25000"),
            current_session_no=0,
            operator_reason="initial live funding for live-small cutover",
            now_utc=_now(),
        )
        assert (
            plan.audit_events[0].payload["operator_reason"]
            == "initial live funding for live-small cutover"
        )
        assert (
            plan.audit_events[1].payload["operator_reason"]
            == "initial live funding for live-small cutover"
        )

    def test_mode_started_payload_carries_session_window(self) -> None:
        plan = plan_capital_event(
            account_id=uuid4(),
            event_type="deposit",
            amount_usd=Decimal("5000"),
            pre_event_equity=Decimal("25000"),
            current_session_no=100,
            operator_reason="",
            now_utc=_now(),
        )
        mode_payload = plan.audit_events[1].payload
        assert mode_payload["mode_active_until_session_no"] == 130
        assert mode_payload["mode_vol_normalized_at_session_no"] == 105
