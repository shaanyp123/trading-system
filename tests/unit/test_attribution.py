"""Unit tests for :mod:`services.risk.attribution`.

Pure-policy tests for the planner; mocked-I/O tests for
``apply_attribution_plan``. A22 N/A (no testcontainers;
``append_audit_event`` is monkeypatched).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.audit.event_types import AuditEventType
from services.risk.attribution import (
    BPS_QUANTIZER,
    PNL_QUANTIZER,
    AttributionError,
    AttributionRow,
    ClosedTradeForAttribution,
    apply_attribution_plan,
    plan_daily_attribution,
)

_ACCOUNT_ID = uuid4()
_SIGNAL_ID = uuid4()
_TRADE_ID = uuid4()
_OPEN_AT = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
_CLOSE_AT = datetime(2026, 5, 13, 15, 30, 0, tzinfo=UTC)
_EXPECTED_AT = datetime(2026, 5, 12, 13, 55, 0, tzinfo=UTC)


def _trade(**overrides: Any) -> ClosedTradeForAttribution:
    base = dict(
        trade_id=_TRADE_ID,
        entry_signal_id=_SIGNAL_ID,
        direction="long",
        total_quantity=2,
        avg_entry_price=Decimal("5230.50"),
        avg_exit_price=Decimal("5245.75"),
        realized_pnl_usd=Decimal("30.50"),
        realized_commission_usd=Decimal("2.00"),
        opened_at_utc=_OPEN_AT,
        closed_at_utc=_CLOSE_AT,
        expected_entry_price=Decimal("5230.00"),
        expected_exit_price=Decimal("5245.00"),
        expected_slippage_bps=Decimal("1.5"),
        expected_holding_days=1,
        expected_at_utc=_EXPECTED_AT,
    )
    base.update(overrides)
    return ClosedTradeForAttribution(**base)  # type: ignore[arg-type]


class TestLockedConstants:
    def test_pnl_quantizer(self) -> None:
        assert PNL_QUANTIZER == Decimal("0.0001")

    def test_bps_quantizer(self) -> None:
        assert BPS_QUANTIZER == Decimal("0.0001")


class TestPlanEmptyDay:
    def test_no_trades_emits_audit_with_zeros(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert plan.rows == ()
        payload = plan.audit_event_payload
        assert payload["trade_count"] == 0
        assert payload["long_count"] == 0
        assert payload["short_count"] == 0
        assert payload["gross_realized_pnl_usd"] == "0.0000"
        assert payload["gross_expected_pnl_usd"] == "0.0000"
        assert payload["session_date_et"] == "2026-05-13"


class TestPlanSingleTrade:
    def test_long_trade_row_shape(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(_trade(),),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert len(plan.rows) == 1
        row = plan.rows[0]
        assert isinstance(row, AttributionRow)
        assert row.trade_id == _TRADE_ID
        assert row.account_id == _ACCOUNT_ID
        assert row.env == "paper"
        assert row.expected_entry_price == Decimal("5230.00")
        assert row.expected_exit_price == Decimal("5245.00")
        assert row.realized_entry_price == Decimal("5230.50")
        assert row.realized_exit_price == Decimal("5245.75")
        assert row.realized_holding_days == 1

    def test_audit_aggregate_matches_single_trade(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(_trade(),),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        p = plan.audit_event_payload
        assert p["trade_count"] == 1
        assert p["long_count"] == 1
        assert p["short_count"] == 0
        assert p["gross_realized_pnl_usd"] == "30.5000"

    def test_realized_pnl_quantized(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(_trade(realized_pnl_usd=Decimal("123.45678901")),),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert plan.rows[0].realized_pnl_usd == Decimal("123.4568")

    def test_holding_days_same_day(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(
                _trade(
                    opened_at_utc=datetime(2026, 5, 13, 14, 0, tzinfo=UTC),
                    closed_at_utc=datetime(2026, 5, 13, 15, 30, tzinfo=UTC),
                ),
            ),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert plan.rows[0].realized_holding_days == 0


class TestPlanShortTrade:
    def test_short_counted_in_audit(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(_trade(direction="short"),),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert plan.audit_event_payload["long_count"] == 0
        assert plan.audit_event_payload["short_count"] == 1


class TestSlippageMath:
    def test_long_entry_slip_positive_when_paid_more(self) -> None:
        from services.risk.attribution import _compute_realized_slippage_bps

        # Long entry: realized > expected → trader paid more → +bps.
        # (100 → 100.5) on a 100-base = 50 bps.
        bps = _compute_realized_slippage_bps(
            direction="long",
            expected_price=Decimal("100"),
            realized_price=Decimal("100.5"),
        )
        assert bps == Decimal("50.0000")

    def test_short_entry_slip_positive_when_received_less(self) -> None:
        from services.risk.attribution import _compute_realized_slippage_bps

        # Short entry: realized < expected → received less → +bps.
        # (100 → 99.5) on 100-base = (-50) raw, flipped to +50 by short logic.
        bps = _compute_realized_slippage_bps(
            direction="short",
            expected_price=Decimal("100"),
            realized_price=Decimal("99.5"),
        )
        assert bps == Decimal("50.0000")

    def test_realized_slip_averaged_when_expected_exit_present(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(
                _trade(
                    direction="long",
                    avg_entry_price=Decimal("100.5"),
                    expected_entry_price=Decimal("100"),
                    avg_exit_price=Decimal("99.5"),  # closed below expected exit
                    expected_exit_price=Decimal("100"),
                ),
            ),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        # entry slip = +50 bps; exit slip (sell side, direction flips
        # to short): expected 100, realized 99.5 → received less → +50 bps.
        # avg = +50 bps.
        assert plan.rows[0].realized_slippage_bps == Decimal("50.0000")

    def test_realized_slip_uses_only_entry_when_expected_exit_none(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(
                _trade(
                    direction="long",
                    avg_entry_price=Decimal("100.5"),
                    expected_entry_price=Decimal("100"),
                    expected_exit_price=None,  # stop-out: no expected exit
                ),
            ),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert plan.rows[0].realized_slippage_bps == Decimal("50.0000")


class TestPlanValidation:
    def test_naive_rollup_rejected(self) -> None:
        with pytest.raises(AttributionError, match="tz-aware UTC"):
            plan_daily_attribution(
                account_id=_ACCOUNT_ID,
                env="paper",
                session_date_et=date(2026, 5, 13),
                closed_trades_today=(),
                rollup_at_utc=datetime(2026, 5, 13, 22, 30),  # naive
            )

    def test_naive_trade_datetime_rejected(self) -> None:
        with pytest.raises(AttributionError, match="tz-aware UTC"):
            plan_daily_attribution(
                account_id=_ACCOUNT_ID,
                env="paper",
                session_date_et=date(2026, 5, 13),
                closed_trades_today=(_trade(opened_at_utc=datetime(2026, 5, 13, 14, 0)),),
                rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
            )

    def test_zero_total_quantity_rejected(self) -> None:
        with pytest.raises(AttributionError, match="total_quantity"):
            plan_daily_attribution(
                account_id=_ACCOUNT_ID,
                env="paper",
                session_date_et=date(2026, 5, 13),
                closed_trades_today=(_trade(total_quantity=0),),
                rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
            )


class TestMultiTradeAggregate:
    def test_two_longs_aggregated(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(
                _trade(trade_id=uuid4(), realized_pnl_usd=Decimal("100")),
                _trade(trade_id=uuid4(), realized_pnl_usd=Decimal("-25.50")),
            ),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert len(plan.rows) == 2
        assert plan.audit_event_payload["trade_count"] == 2
        assert plan.audit_event_payload["long_count"] == 2
        assert plan.audit_event_payload["gross_realized_pnl_usd"] == "74.5000"

    def test_mixed_long_short_counted(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(
                _trade(trade_id=uuid4(), direction="long"),
                _trade(trade_id=uuid4(), direction="short"),
                _trade(trade_id=uuid4(), direction="short"),
            ),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        assert plan.audit_event_payload["long_count"] == 1
        assert plan.audit_event_payload["short_count"] == 2


class TestApplyAttributionPlan:
    """Mocked-I/O tests: audit-first + INSERT count match plan.rows."""

    async def test_apply_empty_plan_still_writes_audit(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )

        # Mock session_factory + append_audit_event.
        session = MagicMock()
        session.execute = AsyncMock()

        @asynccontextmanager
        async def factory():
            yield session

        audit_uuid = uuid4()
        audit_record = MagicMock()
        audit_record.event_uuid = audit_uuid
        audit_record.sequence_no = 1

        with patch(
            "services.risk.attribution.append_audit_event",
            new=AsyncMock(return_value=audit_record),
        ):
            result = await apply_attribution_plan(
                plan,
                session_factory=factory,  # type: ignore[arg-type]
                env="paper",
            )
        assert result.audit_event_uuid == audit_uuid
        assert result.inserted_row_count == 0

    async def test_apply_single_row_inserts(self) -> None:
        plan = plan_daily_attribution(
            account_id=_ACCOUNT_ID,
            env="paper",
            session_date_et=date(2026, 5, 13),
            closed_trades_today=(_trade(),),
            rollup_at_utc=datetime(2026, 5, 13, 22, 30, tzinfo=UTC),
        )
        sql_log: list[str] = []

        async def execute(stmt: Any, params: Any = None) -> MagicMock:
            sql_log.append(str(stmt))
            return MagicMock()

        @asynccontextmanager
        async def _begin_cm():
            yield None

        session = MagicMock()
        session.execute = execute  # type: ignore[method-assign]
        session.begin = MagicMock(side_effect=lambda: _begin_cm())

        @asynccontextmanager
        async def factory():
            yield session

        audit_record = MagicMock()
        audit_record.event_uuid = uuid4()
        audit_record.sequence_no = 1

        with patch(
            "services.risk.attribution.append_audit_event",
            new=AsyncMock(return_value=audit_record),
        ):
            result = await apply_attribution_plan(
                plan,
                session_factory=factory,  # type: ignore[arg-type]
                env="paper",
            )
        assert result.inserted_row_count == 1
        assert any("INSERT INTO attribution" in s for s in sql_log)


class TestModuleContract:
    def test_public_surface(self) -> None:
        from services.risk import attribution as mod

        expected = {
            "BPS_QUANTIZER",
            "PNL_QUANTIZER",
            "AttributionApplyResult",
            "AttributionError",
            "AttributionPlan",
            "AttributionRow",
            "ClosedTradeForAttribution",
            "apply_attribution_plan",
            "plan_daily_attribution",
        }
        assert set(mod.__all__) == expected

    def test_new_audit_event_resolvable(self) -> None:
        et = AuditEventType["ATTRIBUTION_ROLLUP_RECORDED"]
        assert AuditEventType(et.value) is et
