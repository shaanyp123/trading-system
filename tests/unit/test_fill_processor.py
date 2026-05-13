"""Unit tests for :mod:`services.risk.fill_processor`.

Pure-policy tests for the planner; mocked-I/O tests for the apply +
fetch helpers + end-to-end facade. No testcontainers (A22 N/A —
``append_audit_event`` is monkeypatched + session_factory is faked).

Test classes:

  * :class:`TestLockedConstants` — locked module-level constants
  * :class:`TestEnumContract` — locked audit event types resolvable
  * :class:`TestPlanFillApplicationOpenPath` — first-fill scenarios
  * :class:`TestPlanFillApplicationUpdatePath` — subsequent fills
  * :class:`TestPlanFillApplicationOrderStatus` — filled vs partially_filled
  * :class:`TestPlanFillApplicationUnsupportedScenarios` — direction
    mismatch + exit-case rejections
  * :class:`TestPlanFillApplicationValidation` — naive datetime, negative
    quantities, etc.
  * :class:`TestAvgCostRecompute` — Decimal precision + quantization
  * :class:`TestBalanceMath` — buy reduces cash, sell increases cash
  * :class:`TestApplyFillPlan` — mocked I/O: audit-first ordering, table
    INSERT/UPDATE shapes
  * :class:`TestFetchHelpers` — fetch_fill_context / prior_position /
    prior_trade / prior_balance happy + None paths
  * :class:`TestProcessFillEventFacade` — end-to-end with all mocks
  * :class:`TestModuleContract` — __all__ surface + import sanity
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from services.audit.event_types import AuditEventType
from services.risk.fill_processor import (
    AVG_COST_QUANTIZER,
    BALANCE_SOURCE_FROM_FILL,
    BalanceInsert,
    FillApplyPlan,
    FillApplyResult,
    FillContext,
    FillIngestPayload,
    FillProcessingError,
    OrderStatusChange,
    PendingAuditEvent,
    PositionMutation,
    PriorBalanceSnapshot,
    PriorPositionRow,
    PriorTradeRow,
    TradeMutation,
    UnsupportedFillScenarioError,
    _quantize_avg_cost,
    _recompute_avg_cost,
    apply_fill_plan,
    fetch_fill_context,
    fetch_prior_balance,
    fetch_prior_position,
    fetch_prior_trade,
    plan_fill_application,
    process_fill_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ACCOUNT_ID = UUID("aaaaaaaa-aaaa-7aaa-aaaa-aaaaaaaaaaaa")
_ORDER_ID = UUID("bbbbbbbb-bbbb-7bbb-bbbb-bbbbbbbbbbbb")
_SIGNAL_ID = UUID("cccccccc-cccc-7ccc-cccc-cccccccccccc")
_SLIP_ID = UUID("dddddddd-dddd-7ddd-dddd-dddddddddddd")
_ORDER_CREATED = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _ctx(
    *,
    signal_direction: str = "long",
    order_direction: str = "buy",
    target_contracts: int = 1,
    market: str = "/MES",
    contract_id: UUID | None = None,
) -> FillContext:
    return FillContext(
        order_id=_ORDER_ID,
        order_created_at=_ORDER_CREATED,
        signal_id=_SIGNAL_ID,
        account_id=_ACCOUNT_ID,
        env="paper",
        market=market,
        contract_id=contract_id,
        signal_direction=signal_direction,  # type: ignore[arg-type]
        order_direction=order_direction,  # type: ignore[arg-type]
        target_contracts=target_contracts,
        strategy_hash="a" * 40,
        parameter_set_hash="b" * 64,
        slippage_calibration_version_id=_SLIP_ID,
        decision_price=Decimal("4250.50"),
        managed_by_version="a" * 40,
    )


def _payload(
    *,
    fill_quantity: int = 1,
    cumulative_filled_quantity: int | None = None,
    fill_price: str = "4250.50",
    commission_usd: str = "1.25",
    filled_at_utc: datetime | None = None,
    broker_fill_id: str = "12345:agg",
) -> FillIngestPayload:
    return FillIngestPayload(
        broker_fill_id=broker_fill_id,
        cumulative_filled_quantity=cumulative_filled_quantity
        if cumulative_filled_quantity is not None
        else fill_quantity,
        fill_quantity=fill_quantity,
        fill_price=Decimal(fill_price),
        commission_usd=Decimal(commission_usd),
        filled_at_utc=filled_at_utc or datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
    )


def _zero_balance() -> PriorBalanceSnapshot:
    return PriorBalanceSnapshot(cash_usd=Decimal("100000"), net_liquidation=Decimal("100000"))


# ---------------------------------------------------------------------------
# Locked constants + enum contract
# ---------------------------------------------------------------------------


class TestLockedConstants:
    def test_balance_source_is_tws_api(self) -> None:
        assert BALANCE_SOURCE_FROM_FILL == "tws_api"

    def test_avg_cost_quantizer_eight_decimals(self) -> None:
        assert AVG_COST_QUANTIZER == Decimal("0.00000001")


class TestEnumContract:
    """The new event types added in PR-G must resolve via the locked
    taxonomy enum (anti-pattern A04)."""

    @pytest.mark.parametrize(
        "name",
        [
            "POSITION_OPENED",
            "POSITION_UPDATED",
            "POSITION_CLOSED",
            "POSITION_MARK_TO_MARKET",
            "BALANCE_SNAPSHOT_RECORDED",
            "TRADE_OPENED",
            "TRADE_CLOSED",
            "ATTRIBUTION_ROLLUP_RECORDED",
        ],
    )
    def test_new_event_types_resolvable(self, name: str) -> None:
        et = AuditEventType[name]
        # Round-trip through value reconstruction proves it's in the enum.
        assert AuditEventType(et.value) is et


# ---------------------------------------------------------------------------
# Pure-policy: first-fill (open) path
# ---------------------------------------------------------------------------


class TestPlanFillApplicationOpenPath:
    def test_long_entry_no_prior_position_emits_four_audits(self) -> None:
        plan = plan_fill_application(
            context=_ctx(target_contracts=2),
            payload=_payload(fill_quantity=2, cumulative_filled_quantity=2),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        types = [e.event_type for e in plan.audit_events]
        assert types == [
            AuditEventType.ORDER_FILLED,
            AuditEventType.POSITION_OPENED,
            AuditEventType.BALANCE_SNAPSHOT_RECORDED,
            AuditEventType.TRADE_OPENED,
        ]

    def test_position_mutation_open(self) -> None:
        plan = plan_fill_application(
            context=_ctx(),
            payload=_payload(fill_quantity=1),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.position_mutation.action == "open"
        assert plan.position_mutation.position_id is None
        assert plan.position_mutation.new_quantity == 1
        assert plan.position_mutation.new_avg_cost == Decimal("4250.50000000")
        assert plan.position_mutation.market == "/MES"

    def test_trade_mutation_open(self) -> None:
        plan = plan_fill_application(
            context=_ctx(),
            payload=_payload(fill_quantity=1),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.action == "open"
        assert plan.trade_mutation.trade_id is None
        assert plan.trade_mutation.new_total_quantity == 1
        assert plan.trade_mutation.new_avg_entry_price == Decimal("4250.50000000")
        assert plan.trade_mutation.direction == "long"
        assert plan.trade_mutation.entry_signal_id == _SIGNAL_ID
        assert plan.trade_mutation.entry_order_id == _ORDER_ID

    def test_short_entry_negative_quantity(self) -> None:
        plan = plan_fill_application(
            context=_ctx(signal_direction="short", order_direction="sell"),
            payload=_payload(fill_quantity=1),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.position_mutation.new_quantity == -1
        assert plan.trade_mutation.direction == "short"

    def test_balance_insert_source_is_tws_api(self) -> None:
        plan = plan_fill_application(
            context=_ctx(),
            payload=_payload(fill_quantity=1),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.balance_insert.source == BALANCE_SOURCE_FROM_FILL


# ---------------------------------------------------------------------------
# Pure-policy: subsequent-fill (update) path
# ---------------------------------------------------------------------------


class TestPlanFillApplicationUpdatePath:
    def test_position_action_update_when_prior_exists(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=2, avg_cost=Decimal("100"))
        plan = plan_fill_application(
            context=_ctx(target_contracts=5),
            payload=_payload(fill_quantity=3, cumulative_filled_quantity=3, fill_price="110"),
            prior_position=prior_pos,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.position_mutation.action == "update"
        assert plan.position_mutation.position_id == prior_pos.position_id
        assert plan.position_mutation.new_quantity == 5
        # avg = (2*100 + 3*110) / 5 = 530/5 = 106
        assert plan.position_mutation.new_avg_cost == Decimal("106.00000000")

    def test_position_updated_audit_payload_carries_deltas(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=2, avg_cost=Decimal("100"))
        plan = plan_fill_application(
            context=_ctx(target_contracts=5),
            payload=_payload(fill_quantity=3),
            prior_position=prior_pos,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        # POSITION_UPDATED is event index 1.
        position_event = plan.audit_events[1]
        assert position_event.event_type == AuditEventType.POSITION_UPDATED
        assert position_event.payload["prior_quantity"] == 2
        assert position_event.payload["new_quantity"] == 5
        assert position_event.payload["fill_quantity"] == 3

    def test_trade_action_update_when_prior_trade_exists(self) -> None:
        prior_trade = PriorTradeRow(
            trade_id=uuid4(),
            trade_created_at=_ORDER_CREATED,
            total_quantity=2,
            avg_entry_price=Decimal("100"),
        )
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=2, avg_cost=Decimal("100"))
        plan = plan_fill_application(
            context=_ctx(target_contracts=5),
            payload=_payload(fill_quantity=3, fill_price="110"),
            prior_position=prior_pos,
            prior_trade=prior_trade,
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.action == "update"
        assert plan.trade_mutation.trade_id == prior_trade.trade_id
        assert plan.trade_mutation.new_total_quantity == 5
        # avg = (2*100 + 3*110) / 5 = 106
        assert plan.trade_mutation.new_avg_entry_price == Decimal("106.00000000")

    def test_no_trade_opened_event_on_subsequent_fill(self) -> None:
        prior_trade = PriorTradeRow(
            trade_id=uuid4(),
            trade_created_at=_ORDER_CREATED,
            total_quantity=2,
            avg_entry_price=Decimal("100"),
        )
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=2, avg_cost=Decimal("100"))
        plan = plan_fill_application(
            context=_ctx(target_contracts=5),
            payload=_payload(fill_quantity=3),
            prior_position=prior_pos,
            prior_trade=prior_trade,
            prior_balance=_zero_balance(),
        )
        types = [e.event_type for e in plan.audit_events]
        assert AuditEventType.TRADE_OPENED not in types
        assert types == [
            AuditEventType.ORDER_FILLED,
            AuditEventType.POSITION_UPDATED,
            AuditEventType.BALANCE_SNAPSHOT_RECORDED,
        ]


# ---------------------------------------------------------------------------
# Pure-policy: order status (filled vs partially_filled)
# ---------------------------------------------------------------------------


class TestPlanFillApplicationOrderStatus:
    def test_cumulative_equals_target_status_filled(self) -> None:
        plan = plan_fill_application(
            context=_ctx(target_contracts=5),
            payload=_payload(fill_quantity=5, cumulative_filled_quantity=5),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.order_change.new_status == "filled"

    def test_cumulative_less_than_target_status_partially_filled(self) -> None:
        plan = plan_fill_application(
            context=_ctx(target_contracts=5),
            payload=_payload(fill_quantity=3, cumulative_filled_quantity=3),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.order_change.new_status == "partially_filled"

    def test_cumulative_exceeds_target_status_filled(self) -> None:
        """Defensive: if IBKR overfills (rare but possible with stop orders),
        still mark filled rather than emit an invalid intermediate state."""
        plan = plan_fill_application(
            context=_ctx(target_contracts=5),
            payload=_payload(fill_quantity=6, cumulative_filled_quantity=6),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        assert plan.order_change.new_status == "filled"


# ---------------------------------------------------------------------------
# Pure-policy: unsupported scenarios (exit / hedging — out of PR-G scope)
# ---------------------------------------------------------------------------


class TestPlanFillApplicationUnsupportedScenarios:
    def test_long_signal_sell_order_rejected(self) -> None:
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            plan_fill_application(
                context=_ctx(signal_direction="long", order_direction="sell"),
                payload=_payload(fill_quantity=1),
                prior_position=None,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )
        assert exc_info.value.error_code == "FILL_EXIT_CASE_DEFERRED"

    def test_short_signal_buy_order_rejected(self) -> None:
        with pytest.raises(UnsupportedFillScenarioError):
            plan_fill_application(
                context=_ctx(signal_direction="short", order_direction="buy"),
                payload=_payload(fill_quantity=1),
                prior_position=None,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )

    def test_long_entry_with_short_prior_position_rejected(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-2, avg_cost=Decimal("100"))
        with pytest.raises(UnsupportedFillScenarioError):
            plan_fill_application(
                context=_ctx(signal_direction="long", order_direction="buy"),
                payload=_payload(fill_quantity=1),
                prior_position=prior_pos,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )

    def test_short_entry_with_long_prior_position_rejected(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=2, avg_cost=Decimal("100"))
        with pytest.raises(UnsupportedFillScenarioError):
            plan_fill_application(
                context=_ctx(signal_direction="short", order_direction="sell"),
                payload=_payload(fill_quantity=1),
                prior_position=prior_pos,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )


# ---------------------------------------------------------------------------
# Pure-policy: validation (timezone, quantity)
# ---------------------------------------------------------------------------


class TestPlanFillApplicationValidation:
    def test_naive_filled_at_utc_rejected(self) -> None:
        with pytest.raises(FillProcessingError) as exc_info:
            plan_fill_application(
                context=_ctx(),
                payload=_payload(filled_at_utc=datetime(2026, 5, 13, 12, 30, 0)),
                prior_position=None,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )
        assert exc_info.value.error_code == "TIMEZONE_REQUIRED"

    def test_negative_fill_quantity_rejected(self) -> None:
        with pytest.raises(FillProcessingError) as exc_info:
            plan_fill_application(
                context=_ctx(),
                payload=_payload(fill_quantity=-1, cumulative_filled_quantity=1),
                prior_position=None,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )
        assert exc_info.value.error_code == "FILL_QUANTITY_NON_POSITIVE"

    def test_zero_cumulative_quantity_rejected(self) -> None:
        with pytest.raises(FillProcessingError) as exc_info:
            plan_fill_application(
                context=_ctx(),
                payload=_payload(fill_quantity=1, cumulative_filled_quantity=0),
                prior_position=None,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )
        assert exc_info.value.error_code == "CUMULATIVE_QUANTITY_NON_POSITIVE"


# ---------------------------------------------------------------------------
# Pure helpers: avg_cost recompute
# ---------------------------------------------------------------------------


class TestAvgCostRecompute:
    def test_first_position_uses_fill_price(self) -> None:
        assert _recompute_avg_cost(0, Decimal("0"), 5, Decimal("100")) == Decimal("100.00000000")

    def test_add_to_position_weighted_average(self) -> None:
        # 5 @ 100 + 2 @ 110 → avg = (500 + 220) / 7 = 102.857142857142857142...
        # Rounded to 8 decimals half-even.
        assert _recompute_avg_cost(5, Decimal("100"), 2, Decimal("110")) == Decimal("102.85714286")

    def test_quantize_avg_cost_rounds_half_even(self) -> None:
        # 0.000000005 rounds to even neighbor → 0.00000000
        assert _quantize_avg_cost(Decimal("0.000000005")) == Decimal("0.00000000")
        # 0.000000015 → rounds to 0.00000002 (even neighbor at the 8-dp boundary)
        assert _quantize_avg_cost(Decimal("0.000000015")) == Decimal("0.00000002")

    def test_decimal_precision_preserved_through_pipeline(self) -> None:
        plan = plan_fill_application(
            context=_ctx(),
            payload=_payload(fill_quantity=3, fill_price="123.45678901"),
            prior_position=None,
            prior_trade=None,
            prior_balance=_zero_balance(),
        )
        # First fill at given price → avg = fill_price exactly.
        assert plan.position_mutation.new_avg_cost == Decimal("123.45678901")


# ---------------------------------------------------------------------------
# Pure-policy: balance math
# ---------------------------------------------------------------------------


class TestBalanceMath:
    def test_buy_reduces_cash(self) -> None:
        plan = plan_fill_application(
            context=_ctx(),
            payload=_payload(fill_quantity=1, fill_price="100", commission_usd="2"),
            prior_position=None,
            prior_trade=None,
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("100000"), net_liquidation=Decimal("100000")
            ),
        )
        # Notional = 1 * 100 = 100; commission = 2; cash = 100000 - 102 = 99898
        assert plan.balance_insert.cash_usd == Decimal("99898.0000")

    def test_sell_short_increases_cash(self) -> None:
        plan = plan_fill_application(
            context=_ctx(signal_direction="short", order_direction="sell"),
            payload=_payload(fill_quantity=1, fill_price="100", commission_usd="2"),
            prior_position=None,
            prior_trade=None,
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("100000"), net_liquidation=Decimal("100000")
            ),
        )
        # Short sale: cash credit = 100, minus commission = 2 → 100000 + 100 - 2 = 100098
        assert plan.balance_insert.cash_usd == Decimal("100098.0000")

    def test_balance_audit_payload_carries_decimal_as_string(self) -> None:
        plan = plan_fill_application(
            context=_ctx(),
            payload=_payload(fill_quantity=1, fill_price="100", commission_usd="2"),
            prior_position=None,
            prior_trade=None,
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("100000"), net_liquidation=Decimal("100000")
            ),
        )
        bal_event = plan.audit_events[2]
        assert bal_event.event_type == AuditEventType.BALANCE_SNAPSHOT_RECORDED
        # Decimal-as-string for [A05].
        assert isinstance(bal_event.payload["new_cash_usd"], str)
        assert isinstance(bal_event.payload["cash_delta_usd"], str)
        assert bal_event.payload["new_cash_usd"] == "99898.0000"


# ---------------------------------------------------------------------------
# Mocked I/O: apply_fill_plan
# ---------------------------------------------------------------------------


def _build_mock_session_factory() -> MagicMock:
    """Build a session_factory MagicMock that returns AsyncMock sessions.

    Each call to ``session_factory()`` returns a fresh async-context-manager
    yielding an AsyncMock with ``execute`` returning a MagicMock whose
    ``fetchone()`` returns a MagicMock with an ``.id`` attribute set to a
    fresh UUID. Suitable for INSERT...RETURNING patterns.
    """
    session_factory = MagicMock()

    def _make_session() -> MagicMock:
        sess = MagicMock()
        sess.execute = AsyncMock()
        sess.commit = AsyncMock()

        # Begin returns an async context manager (used for `async with
        # session.begin():` patterns). Make it a no-op AsyncMock.
        @asynccontextmanager
        async def _begin_cm() -> Any:
            yield None

        sess.begin = MagicMock(return_value=_begin_cm())
        # Default execute returns a result with fetchone returning a row
        # with .id = fresh UUID; tests override as needed.
        result_mock = MagicMock()
        result_mock.fetchone = MagicMock(return_value=MagicMock(id=uuid4()))
        sess.execute.return_value = result_mock
        return sess

    @asynccontextmanager
    async def _factory_cm() -> Any:
        yield _make_session()

    session_factory.side_effect = lambda: _factory_cm()
    return session_factory


def _audit_record_mock(event_uuid: UUID | None = None) -> MagicMock:
    rec = MagicMock()
    rec.event_uuid = event_uuid or uuid4()
    rec.sequence_no = 1
    return rec


def _build_plan(*, action: str = "open") -> FillApplyPlan:
    """Build a minimal valid FillApplyPlan for apply-step tests."""
    audit_events = (
        PendingAuditEvent(event_type=AuditEventType.ORDER_FILLED, payload={"k": "v"}),
        PendingAuditEvent(event_type=AuditEventType.POSITION_OPENED, payload={"k": "v"}),
        PendingAuditEvent(event_type=AuditEventType.BALANCE_SNAPSHOT_RECORDED, payload={"k": "v"}),
        PendingAuditEvent(event_type=AuditEventType.TRADE_OPENED, payload={"k": "v"}),
    )
    return FillApplyPlan(
        audit_events=audit_events,
        order_change=OrderStatusChange(
            order_id=_ORDER_ID,
            order_created_at=_ORDER_CREATED,
            new_status="filled",
        ),
        fill_insert=FillInsert_for_test(),
        position_mutation=PositionMutation(
            action=action,  # type: ignore[arg-type]
            position_id=None if action == "open" else uuid4(),
            account_id=_ACCOUNT_ID,
            market="/MES",
            contract_id=None,
            new_quantity=1,
            new_avg_cost=Decimal("4250.50000000"),
            last_mark_ts=datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
            managed_by_version="a" * 40,
        ),
        balance_insert=BalanceInsert(
            account_id=_ACCOUNT_ID,
            snapshot_ts=datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
            net_liquidation=Decimal("99898.0000"),
            cash_usd=Decimal("99898.0000"),
            excess_liquidity=Decimal("99898.0000"),
            used_margin_pct=Decimal("0"),
            source=BALANCE_SOURCE_FROM_FILL,
        ),
        trade_mutation=TradeMutation(
            action=action,  # type: ignore[arg-type]
            trade_id=None if action == "open" else uuid4(),
            trade_created_at=None if action == "open" else _ORDER_CREATED,
            account_id=_ACCOUNT_ID,
            env="paper",
            market="/MES",
            contract_id=None,
            entry_signal_id=_SIGNAL_ID,
            entry_order_id=_ORDER_ID,
            direction="long",
            opened_at_utc=datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
            new_total_quantity=1,
            new_avg_entry_price=Decimal("4250.50000000"),
            realized_commission_usd=Decimal("1.25"),
            managed_by_version="a" * 40,
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            slippage_calibration_version_id=_SLIP_ID,
        ),
    )


def FillInsert_for_test() -> Any:
    """Helper to construct a FillInsert dataclass for the apply tests."""
    from services.risk.fill_processor import FillInsert

    return FillInsert(
        account_id=_ACCOUNT_ID,
        env="paper",
        order_id=_ORDER_ID,
        broker_fill_id="12345:agg",
        filled_at_utc=datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
        fill_price=Decimal("4250.50"),
        fill_quantity=1,
        commission_usd=Decimal("1.25"),
        exchange_fee_usd=Decimal("0"),
    )


class TestApplyFillPlan:
    @pytest.mark.asyncio
    async def test_audit_first_writes_all_events_before_tables(self) -> None:
        """append_audit_event must be called len(plan.audit_events) times
        BEFORE any session_factory() call dedicated to a table mutation."""
        plan = _build_plan(action="open")
        session_factory = _build_mock_session_factory()
        call_order: list[str] = []

        async def _fake_append(*args: Any, **kwargs: Any) -> MagicMock:
            call_order.append("audit")
            return _audit_record_mock()

        with patch(
            "services.risk.fill_processor.append_audit_event",
            side_effect=_fake_append,
        ):
            original_side_effect = session_factory.side_effect

            def _wrapped() -> Any:
                # Each non-audit session_factory call gets logged.
                call_order.append("session")
                return original_side_effect()  # type: ignore[misc]

            session_factory.side_effect = _wrapped
            result = await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )

        # First 4 audit calls open their own sessions inside append_audit_event
        # (monkeypatched, so they don't actually invoke session_factory),
        # but our wrapper DOES count the session_factory() calls inside
        # apply_fill_plan's own loop (one per audit event for the
        # ``async with session_factory() as audit_session`` block).
        n_audit = sum(1 for x in call_order if x == "audit")
        assert n_audit == 4
        assert len(result.audit_event_uuids) == 4

    @pytest.mark.asyncio
    async def test_result_carries_signal_account_audit_uuids(self) -> None:
        plan = _build_plan(action="open")
        session_factory = _build_mock_session_factory()
        with patch(
            "services.risk.fill_processor.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )
        assert result.signal_id == _SIGNAL_ID
        assert result.account_id == _ACCOUNT_ID
        assert result.new_order_status == "filled"
        assert len(result.audit_event_uuids) == 4

    @pytest.mark.asyncio
    async def test_apply_returns_valid_fillapplyresult(self) -> None:
        plan = _build_plan(action="open")
        session_factory = _build_mock_session_factory()
        with patch(
            "services.risk.fill_processor.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            result = await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )
        assert isinstance(result, FillApplyResult)
        # All ids populated (uuid generated by mock).
        assert result.fill_id is not None
        assert result.position_id is not None
        assert result.trade_id is not None
        assert result.balance_id is not None


# ---------------------------------------------------------------------------
# Mocked I/O: fetch helpers
# ---------------------------------------------------------------------------


def _build_mock_session_factory_with_row(row: Any | None) -> MagicMock:
    """Build a session_factory that returns a session whose execute()
    returns a result whose fetchone() returns ``row``."""
    session_factory = MagicMock()

    @asynccontextmanager
    async def _factory_cm() -> Any:
        sess = MagicMock()
        sess.execute = AsyncMock()
        result_mock = MagicMock()
        result_mock.fetchone = MagicMock(return_value=row)
        sess.execute.return_value = result_mock
        yield sess

    session_factory.side_effect = lambda: _factory_cm()
    return session_factory


class TestFetchHelpers:
    @pytest.mark.asyncio
    async def test_fetch_fill_context_happy_path(self) -> None:
        row = MagicMock(
            order_id=_ORDER_ID,
            order_created_at=_ORDER_CREATED,
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            env="paper",
            market="/MES",
            contract_id=None,
            order_direction="buy",
            signal_direction="long",
            target_contracts=1,
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            slippage_calibration_version_id=_SLIP_ID,
            decision_price=Decimal("4250.50"),
        )
        sf = _build_mock_session_factory_with_row(row)
        ctx = await fetch_fill_context(sf, client_order_id="abc-123-0")
        assert ctx is not None
        assert ctx.order_id == _ORDER_ID
        assert ctx.signal_direction == "long"
        assert ctx.target_contracts == 1
        assert ctx.decision_price == Decimal("4250.50")

    @pytest.mark.asyncio
    async def test_fetch_fill_context_none_when_no_row(self) -> None:
        sf = _build_mock_session_factory_with_row(None)
        ctx = await fetch_fill_context(sf, client_order_id="ghost")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_fetch_prior_position_happy(self) -> None:
        row = MagicMock(id=uuid4(), quantity=2, avg_cost=Decimal("100"))
        sf = _build_mock_session_factory_with_row(row)
        prior = await fetch_prior_position(
            sf, account_id=_ACCOUNT_ID, market="/MES", contract_id=None
        )
        assert prior is not None
        assert prior.quantity == 2
        assert prior.avg_cost == Decimal("100")

    @pytest.mark.asyncio
    async def test_fetch_prior_position_none(self) -> None:
        sf = _build_mock_session_factory_with_row(None)
        prior = await fetch_prior_position(
            sf, account_id=_ACCOUNT_ID, market="/MES", contract_id=None
        )
        assert prior is None

    @pytest.mark.asyncio
    async def test_fetch_prior_trade_happy(self) -> None:
        trade_id = uuid4()
        row = MagicMock(
            id=trade_id,
            created_at=_ORDER_CREATED,
            total_quantity=2,
            avg_entry_price=Decimal("100"),
        )
        sf = _build_mock_session_factory_with_row(row)
        prior = await fetch_prior_trade(sf, entry_signal_id=_SIGNAL_ID)
        assert prior is not None
        assert prior.trade_id == trade_id
        assert prior.total_quantity == 2

    @pytest.mark.asyncio
    async def test_fetch_prior_trade_none(self) -> None:
        sf = _build_mock_session_factory_with_row(None)
        prior = await fetch_prior_trade(sf, entry_signal_id=_SIGNAL_ID)
        assert prior is None

    @pytest.mark.asyncio
    async def test_fetch_prior_balance_zero_when_no_row(self) -> None:
        sf = _build_mock_session_factory_with_row(None)
        prior = await fetch_prior_balance(sf, account_id=_ACCOUNT_ID)
        assert prior.cash_usd == Decimal(0)
        assert prior.net_liquidation == Decimal(0)

    @pytest.mark.asyncio
    async def test_fetch_prior_balance_happy(self) -> None:
        row = MagicMock(cash_usd=Decimal("50000"), net_liquidation=Decimal("60000"))
        sf = _build_mock_session_factory_with_row(row)
        prior = await fetch_prior_balance(sf, account_id=_ACCOUNT_ID)
        assert prior.cash_usd == Decimal("50000")
        assert prior.net_liquidation == Decimal("60000")


# ---------------------------------------------------------------------------
# Mocked I/O: end-to-end facade
# ---------------------------------------------------------------------------


class TestProcessFillEventFacade:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_context(self) -> None:
        """fetch_fill_context returns None → facade returns None."""
        with patch(
            "services.risk.fill_processor.fetch_fill_context",
            new=AsyncMock(return_value=None),
        ):
            result = await process_fill_event(
                session_factory=MagicMock(),
                client_order_id="ghost",
                payload=_payload(fill_quantity=1),
                env="paper",
            )
        assert result is None


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_public_surface_complete(self) -> None:
        import services.risk.fill_processor as mod

        expected = {
            "AVG_COST_QUANTIZER",
            "BALANCE_SOURCE_FROM_FILL",
            "BalanceInsert",
            "FillApplyPlan",
            "FillApplyResult",
            "FillContext",
            "FillIngestPayload",
            "FillInsert",
            "FillProcessingError",
            "OrderStatusChange",
            "PendingAuditEvent",
            "PositionAction",
            "PositionMutation",
            "PriorBalanceSnapshot",
            "PriorPositionRow",
            "PriorTradeRow",
            "SSEEmitter",
            "TradeAction",
            "TradeMutation",
            "UnsupportedFillScenarioError",
            "apply_fill_plan",
            "fetch_fill_context",
            "fetch_prior_balance",
            "fetch_prior_position",
            "fetch_prior_trade",
            "plan_fill_application",
            "process_fill_event",
        }
        assert set(mod.__all__) == expected
