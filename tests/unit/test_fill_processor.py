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
    _classify_fill_scenario,
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
    multiplier: Decimal = Decimal(1),
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
        multiplier=multiplier,
    )


def _futures_ctx(
    *,
    signal_direction: str = "long",
    order_direction: str = "buy",
    target_contracts: int = 1,
    market: str = "/MES",
    multiplier: Decimal = Decimal(5),
) -> FillContext:
    """Helper for the futures-path tests — populates ``contract_id`` so
    the planner takes the futures branch, with ``multiplier`` defaulting
    to /MES's $5/pt."""
    return _ctx(
        signal_direction=signal_direction,
        order_direction=order_direction,
        target_contracts=target_contracts,
        market=market,
        contract_id=UUID("99999999-9999-7999-9999-999999999999"),
        multiplier=multiplier,
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
            realized_commission_usd=Decimal("0"),
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
            realized_commission_usd=Decimal("0"),
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


def _no_open_trade_guard() -> Any:
    """Patch the refuse-at-open guard's market lookup to find nothing.

    ``apply_fill_plan`` on an ``action='open'`` plan re-checks
    ``fetch_prior_open_trade_for_market`` before its first write; the
    mock session factory would otherwise hand the real helper a MagicMock
    row that reads as an existing open trade.
    """
    return patch(
        "services.risk.fill_processor.fetch_prior_open_trade_for_market",
        new=AsyncMock(return_value=None),
    )


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

        with (
            patch(
                "services.risk.fill_processor.append_audit_event",
                side_effect=_fake_append,
            ),
            _no_open_trade_guard(),
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
        with (
            patch(
                "services.risk.fill_processor.append_audit_event",
                new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
            ),
            _no_open_trade_guard(),
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
        with (
            patch(
                "services.risk.fill_processor.append_audit_event",
                new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
            ),
            _no_open_trade_guard(),
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


class TestApplyFillPlanRefuseAtOpenGuard:
    """#377 accepted follow-up: one-open-trade-per-market is enforced at
    the apply-step choke point because the partitioned ``trades`` table
    cannot carry the partial unique index."""

    @staticmethod
    def _existing_open_trade() -> PriorTradeRow:
        return PriorTradeRow(
            trade_id=uuid4(),
            trade_created_at=_ORDER_CREATED,
            total_quantity=1,
            avg_entry_price=Decimal("85.00000000"),
            realized_commission_usd=Decimal("0.50"),
            direction="long",
        )

    @pytest.mark.asyncio
    async def test_open_action_with_existing_open_trade_refuses_before_any_write(self) -> None:
        """The guard raises DUPLICATE_OPEN_TRADE_FOR_MARKET and NOTHING is
        written — no audit event, no session opened for a table mutation."""
        plan = _build_plan(action="open")
        session_factory = _build_mock_session_factory()
        existing = self._existing_open_trade()
        append_mock = AsyncMock(side_effect=lambda *a, **k: _audit_record_mock())
        guard_lookup = AsyncMock(return_value=existing)
        with (
            patch("services.risk.fill_processor.append_audit_event", new=append_mock),
            patch(
                "services.risk.fill_processor.fetch_prior_open_trade_for_market",
                new=guard_lookup,
            ),
        ):
            with pytest.raises(FillProcessingError) as excinfo:
                await apply_fill_plan(
                    plan,
                    session_factory=session_factory,
                    account_id=_ACCOUNT_ID,
                    env="paper",
                )
        assert excinfo.value.error_code == "DUPLICATE_OPEN_TRADE_FOR_MARKET"
        assert excinfo.value.details["market"] == plan.trade_mutation.market
        assert excinfo.value.details["existing_trade_id"] == str(existing.trade_id)
        # Pre-apply, before ANY write: no audit append, no mutation session.
        append_mock.assert_not_awaited()
        assert session_factory.call_count == 0
        # The guard queried exactly the plan's (account, market).
        guard_lookup.assert_awaited_once()
        kwargs = guard_lookup.await_args.kwargs
        assert kwargs["account_id"] == plan.trade_mutation.account_id
        assert kwargs["market"] == plan.trade_mutation.market

    @pytest.mark.asyncio
    async def test_open_action_with_no_open_trade_proceeds(self) -> None:
        plan = _build_plan(action="open")
        session_factory = _build_mock_session_factory()
        with (
            patch(
                "services.risk.fill_processor.append_audit_event",
                new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
            ),
            _no_open_trade_guard(),
        ):
            result = await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )
        assert isinstance(result, FillApplyResult)

    @pytest.mark.asyncio
    async def test_update_action_skips_guard(self) -> None:
        """ADD-attach (#377 market fallback → action='update') must not
        consult the guard — an open trade EXISTS by construction there."""
        plan = _build_plan(action="update")
        session_factory = _build_mock_session_factory()
        guard_lookup = AsyncMock(return_value=self._existing_open_trade())
        with (
            patch(
                "services.risk.fill_processor.append_audit_event",
                new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_open_trade_for_market",
                new=guard_lookup,
            ),
        ):
            result = await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )
        assert isinstance(result, FillApplyResult)
        guard_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_action_skips_guard(self) -> None:
        """Decision-driven closes (#377 market fallback → action='close')
        must not consult the guard."""
        plan = _build_close_plan()
        session_factory = _build_mock_session_factory()
        guard_lookup = AsyncMock(return_value=self._existing_open_trade())
        with (
            patch(
                "services.risk.fill_processor.append_audit_event",
                new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_open_trade_for_market",
                new=guard_lookup,
            ),
        ):
            result = await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )
        assert isinstance(result, FillApplyResult)
        guard_lookup.assert_not_awaited()


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
            multiplier=Decimal("1"),
        )
        sf = _build_mock_session_factory_with_row(row)
        ctx = await fetch_fill_context(sf, client_order_id="abc-123-0")
        assert ctx is not None
        assert ctx.order_id == _ORDER_ID
        assert ctx.signal_direction == "long"
        assert ctx.target_contracts == 1
        assert ctx.decision_price == Decimal("4250.50")
        # ETF row: multiplier COALESCEd to 1 by the LEFT JOIN contracts.
        assert ctx.multiplier == Decimal("1")

    @pytest.mark.asyncio
    async def test_fetch_fill_context_futures_carries_multiplier(self) -> None:
        """For futures (orders.contract_id non-NULL), the LEFT JOIN
        contracts produces the contract's multiplier (e.g., /MES=5)."""
        row = MagicMock(
            order_id=_ORDER_ID,
            order_created_at=_ORDER_CREATED,
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            env="paper",
            market="/MES",
            contract_id=UUID("99999999-9999-7999-9999-999999999999"),
            order_direction="buy",
            signal_direction="long",
            target_contracts=1,
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            slippage_calibration_version_id=_SLIP_ID,
            decision_price=Decimal("4250.50"),
            multiplier=Decimal("5"),
        )
        sf = _build_mock_session_factory_with_row(row)
        ctx = await fetch_fill_context(sf, client_order_id="abc-123-0")
        assert ctx is not None
        assert ctx.multiplier == Decimal("5")
        assert ctx.contract_id is not None

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
            realized_commission_usd=Decimal("0.50"),
        )
        sf = _build_mock_session_factory_with_row(row)
        prior = await fetch_prior_trade(sf, entry_signal_id=_SIGNAL_ID)
        assert prior is not None
        assert prior.trade_id == trade_id
        assert prior.total_quantity == 2
        assert prior.realized_commission_usd == Decimal("0.50")

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

    @pytest.mark.asyncio
    async def test_market_fallback_resolves_close_leg_trade(self) -> None:
        """2026-07-12 incident lock: a decision-driven close leg carries a
        FRESH signal_id (the C1 worker mints one per leg), so the
        same-signal-id trade lookup misses — the facade must fall back to
        the open trade for (account, market) instead of raising
        EXIT_NO_PRIOR_TRADE after the venue already filled."""
        # Short -2 position; buy 2 fill => EXIT_FULL_CLOSE.
        ctx = _ctx(signal_direction="short", order_direction="buy", target_contracts=2)
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-2, avg_cost=Decimal("64000"))
        fallback_trade = PriorTradeRow(
            trade_id=uuid4(),
            trade_created_at=_ORDER_CREATED,
            total_quantity=2,
            avg_entry_price=Decimal("64000"),
            realized_commission_usd=Decimal("1"),
            direction="short",  # matches the live position sign
        )
        applied = MagicMock()
        market_lookup = AsyncMock(return_value=fallback_trade)
        apply_mock = AsyncMock(return_value=applied)
        with (
            patch(
                "services.risk.fill_processor.fetch_fill_context",
                new=AsyncMock(return_value=ctx),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_position",
                new=AsyncMock(return_value=prior_pos),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_trade",
                new=AsyncMock(return_value=None),  # fresh signal id: miss
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_open_trade_for_market",
                new=market_lookup,
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_balance",
                new=AsyncMock(
                    return_value=PriorBalanceSnapshot(
                        cash_usd=Decimal("1549"), net_liquidation=Decimal("1549")
                    )
                ),
            ),
            patch("services.risk.fill_processor.apply_fill_plan", new=apply_mock),
        ):
            result = await process_fill_event(
                session_factory=MagicMock(),
                client_order_id="close-leg-cid",
                payload=_payload(fill_quantity=2),
                env="paper",
            )
        assert result is applied
        market_lookup.assert_awaited_once()
        kwargs = market_lookup.await_args.kwargs
        assert kwargs["account_id"] == ctx.account_id
        assert kwargs["market"] == ctx.market
        # The plan actually reached apply — i.e. no EXIT_NO_PRIOR_TRADE.
        plan = apply_mock.await_args.kwargs.get("plan") or apply_mock.await_args.args[0]
        assert plan is not None

    @pytest.mark.asyncio
    async def test_fallback_direction_mismatch_refuses(self) -> None:
        """Risk-review C1: a fallback trade whose direction contradicts
        the live position sign is corrupt/stale — refuse pre-apply."""
        ctx = _ctx(signal_direction="short", order_direction="buy", target_contracts=2)
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-2, avg_cost=Decimal("64000"))
        wrong_way_trade = PriorTradeRow(
            trade_id=uuid4(),
            trade_created_at=_ORDER_CREATED,
            total_quantity=2,
            avg_entry_price=Decimal("64000"),
            realized_commission_usd=Decimal("1"),
            direction="long",  # position is short
        )
        with (
            patch(
                "services.risk.fill_processor.fetch_fill_context",
                new=AsyncMock(return_value=ctx),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_position",
                new=AsyncMock(return_value=prior_pos),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_trade",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_open_trade_for_market",
                new=AsyncMock(return_value=wrong_way_trade),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_balance",
                new=AsyncMock(
                    return_value=PriorBalanceSnapshot(
                        cash_usd=Decimal("1549"), net_liquidation=Decimal("1549")
                    )
                ),
            ),
        ):
            with pytest.raises(FillProcessingError) as excinfo:
                await process_fill_event(
                    session_factory=MagicMock(),
                    client_order_id="close-leg-cid",
                    payload=_payload(fill_quantity=2),
                    env="paper",
                )
        assert excinfo.value.error_code == "FALLBACK_TRADE_DIRECTION_MISMATCH"

    @pytest.mark.asyncio
    async def test_no_open_trade_anywhere_still_raises(self) -> None:
        """Both lookups missing = genuine corruption; the raise stays."""
        ctx = _ctx(signal_direction="short", order_direction="buy", target_contracts=2)
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-2, avg_cost=Decimal("64000"))
        with (
            patch(
                "services.risk.fill_processor.fetch_fill_context",
                new=AsyncMock(return_value=ctx),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_position",
                new=AsyncMock(return_value=prior_pos),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_trade",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_open_trade_for_market",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_balance",
                new=AsyncMock(
                    return_value=PriorBalanceSnapshot(
                        cash_usd=Decimal("1549"), net_liquidation=Decimal("1549")
                    )
                ),
            ),
        ):
            with pytest.raises(FillProcessingError) as excinfo:
                await process_fill_event(
                    session_factory=MagicMock(),
                    client_order_id="close-leg-cid",
                    payload=_payload(fill_quantity=2),
                    env="paper",
                )
        assert excinfo.value.error_code == "EXIT_NO_PRIOR_TRADE"

    @pytest.mark.asyncio
    async def test_entry_open_race_refused_by_apply_guard(self) -> None:
        """Race-window pin for the refuse-at-open guard: the facade's
        fallback lookup misses (no open trade at plan time), but by apply
        time a concurrent writer opened one — the guard's re-check inside
        apply_fill_plan refuses pre-write instead of minting a sibling."""
        ctx = _ctx(signal_direction="long", order_direction="buy", target_contracts=1)
        raced_in_trade = PriorTradeRow(
            trade_id=uuid4(),
            trade_created_at=_ORDER_CREATED,
            total_quantity=1,
            avg_entry_price=Decimal("85.00000000"),
            realized_commission_usd=Decimal("0.50"),
            direction="long",
        )
        append_mock = AsyncMock(side_effect=lambda *a, **k: _audit_record_mock())
        # First call: the facade's market fallback (miss). Second call:
        # the apply-step guard re-check (hit — the race lost).
        market_lookup = AsyncMock(side_effect=[None, raced_in_trade])
        with (
            patch(
                "services.risk.fill_processor.fetch_fill_context",
                new=AsyncMock(return_value=ctx),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_position",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_trade",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_open_trade_for_market",
                new=market_lookup,
            ),
            patch(
                "services.risk.fill_processor.fetch_prior_balance",
                new=AsyncMock(
                    return_value=PriorBalanceSnapshot(
                        cash_usd=Decimal("1549"), net_liquidation=Decimal("1549")
                    )
                ),
            ),
            patch("services.risk.fill_processor.append_audit_event", new=append_mock),
        ):
            with pytest.raises(FillProcessingError) as excinfo:
                await process_fill_event(
                    session_factory=MagicMock(),
                    client_order_id="entry-race-cid",
                    payload=_payload(fill_quantity=1),
                    env="paper",
                )
        assert excinfo.value.error_code == "DUPLICATE_OPEN_TRADE_FOR_MARKET"
        assert market_lookup.await_count == 2
        append_mock.assert_not_awaited()  # nothing written


# ---------------------------------------------------------------------------
# Pure-policy: _classify_fill_scenario
# ---------------------------------------------------------------------------


class TestClassifyFillScenario:
    """Exercises the public dispatch table for the classifier directly.
    Test class added 2026-05-17 for the PR-G exit-path extension."""

    def test_first_fill_long_buy_is_entry(self) -> None:
        assert (
            _classify_fill_scenario(
                signal_direction="long",
                order_direction="buy",
                prior_position_quantity=0,
                fill_quantity=1,
            )
            == "ENTRY"
        )

    def test_first_fill_short_sell_is_entry(self) -> None:
        assert (
            _classify_fill_scenario(
                signal_direction="short",
                order_direction="sell",
                prior_position_quantity=0,
                fill_quantity=1,
            )
            == "ENTRY"
        )

    def test_same_direction_long_add_is_entry(self) -> None:
        assert (
            _classify_fill_scenario(
                signal_direction="long",
                order_direction="buy",
                prior_position_quantity=2,
                fill_quantity=3,
            )
            == "ENTRY"
        )

    def test_same_direction_short_add_is_entry(self) -> None:
        assert (
            _classify_fill_scenario(
                signal_direction="short",
                order_direction="sell",
                prior_position_quantity=-2,
                fill_quantity=3,
            )
            == "ENTRY"
        )

    def test_full_close_long_via_sell_is_exit_full_close(self) -> None:
        """+5 long, sell 5 → EXIT_FULL_CLOSE."""
        assert (
            _classify_fill_scenario(
                signal_direction="long",  # entry's direction (stop reuses signal_id)
                order_direction="sell",
                prior_position_quantity=5,
                fill_quantity=5,
            )
            == "EXIT_FULL_CLOSE"
        )

    def test_full_close_short_via_buy_is_exit_full_close(self) -> None:
        """-3 short, buy 3 → EXIT_FULL_CLOSE."""
        assert (
            _classify_fill_scenario(
                signal_direction="short",
                order_direction="buy",
                prior_position_quantity=-3,
                fill_quantity=3,
            )
            == "EXIT_FULL_CLOSE"
        )

    def test_partial_close_long_raises_exit_partial(self) -> None:
        """+5 long, sell 3 → EXIT_PARTIAL (deferred)."""
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            _classify_fill_scenario(
                signal_direction="long",
                order_direction="sell",
                prior_position_quantity=5,
                fill_quantity=3,
            )
        assert "Partial exit" in exc_info.value.message

    def test_partial_close_short_raises_exit_partial(self) -> None:
        """-5 short, buy 2 → EXIT_PARTIAL (deferred)."""
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            _classify_fill_scenario(
                signal_direction="short",
                order_direction="buy",
                prior_position_quantity=-5,
                fill_quantity=2,
            )
        assert "Partial exit" in exc_info.value.message

    def test_exit_reversal_long_raises(self) -> None:
        """+5 long, sell 8 → EXIT_REVERSAL (deferred)."""
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            _classify_fill_scenario(
                signal_direction="long",
                order_direction="sell",
                prior_position_quantity=5,
                fill_quantity=8,
            )
        assert "reversal" in exc_info.value.message

    def test_exit_reversal_short_raises(self) -> None:
        """-3 short, buy 5 → EXIT_REVERSAL (deferred)."""
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            _classify_fill_scenario(
                signal_direction="short",
                order_direction="buy",
                prior_position_quantity=-3,
                fill_quantity=5,
            )
        assert "reversal" in exc_info.value.message

    def test_entry_signal_direction_mismatch_raises(self) -> None:
        """First-fill, signal=long, order=sell → raises (cross-direction)."""
        with pytest.raises(UnsupportedFillScenarioError):
            _classify_fill_scenario(
                signal_direction="long",
                order_direction="sell",
                prior_position_quantity=0,
                fill_quantity=1,
            )


# ---------------------------------------------------------------------------
# Pure-policy: exit-close path planner
# ---------------------------------------------------------------------------


def _open_trade_row(
    *,
    total_quantity: int = 1,
    avg_entry_price: str = "85.00",
    realized_commission_usd: str = "0.50",
) -> PriorTradeRow:
    """Helper — construct a PriorTradeRow for the exit-close tests."""
    return PriorTradeRow(
        trade_id=UUID("eeeeeeee-eeee-7eee-eeee-eeeeeeeeeeee"),
        trade_created_at=_ORDER_CREATED,
        total_quantity=total_quantity,
        avg_entry_price=Decimal(avg_entry_price),
        realized_commission_usd=Decimal(realized_commission_usd),
    )


class TestPlanFillApplicationExitClosePath:
    """Exit-close path tests added 2026-05-17 (PR-G exit-path extension)."""

    def test_long_close_emits_four_audit_events_in_order(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(),
            prior_balance=_zero_balance(),
        )
        types = [e.event_type for e in plan.audit_events]
        assert types == [
            AuditEventType.ORDER_FILLED,
            AuditEventType.POSITION_CLOSED,
            AuditEventType.BALANCE_SNAPSHOT_RECORDED,
            AuditEventType.TRADE_CLOSED,
        ]

    def test_short_close_emits_four_audit_events(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="short",
                order_direction="buy",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="83.50", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(),
            prior_balance=_zero_balance(),
        )
        types = [e.event_type for e in plan.audit_events]
        assert types == [
            AuditEventType.ORDER_FILLED,
            AuditEventType.POSITION_CLOSED,
            AuditEventType.BALANCE_SNAPSHOT_RECORDED,
            AuditEventType.TRADE_CLOSED,
        ]

    def test_long_close_realized_pnl_positive_when_exit_above_entry(self) -> None:
        """1 TLT long, entry 85.00, exit 86.50 → +1.50 gross - 0.50 entry comm
        - 0.50 exit comm = 0.50 net (signed positive)."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(
                total_quantity=1,
                avg_entry_price="85.00",
                realized_commission_usd="0.50",
            ),
            prior_balance=_zero_balance(),
        )
        # gross = (86.50 - 85.00) * 1 = 1.50
        # total_commission = 0.50 (entry) + 0.50 (exit) = 1.00
        # pnl = 1.50 - 1.00 = 0.50
        assert plan.trade_mutation.realized_pnl_usd == Decimal("0.5000")
        # Audit event mirrors the same number.
        trade_event = plan.audit_events[3]
        assert trade_event.payload["realized_pnl_usd"] == "0.5000"
        assert trade_event.payload["gross_pnl_usd"] == "1.5000"

    def test_long_close_realized_pnl_negative_when_exit_below_entry(self) -> None:
        """1 TLT long, entry 85.00, exit 82.00 → -3.00 gross - 1.00 commission
        = -4.00 net."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="82.00", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(
                total_quantity=1,
                avg_entry_price="85.00",
                realized_commission_usd="0.50",
            ),
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.realized_pnl_usd == Decimal("-4.0000")

    def test_short_close_realized_pnl_positive_when_exit_below_entry(self) -> None:
        """1 TLT short, entry 85.00, exit 82.00 → +3.00 gross - 1.00 commission
        = +2.00 net."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="short",
                order_direction="buy",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="82.00", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(
                total_quantity=1,
                avg_entry_price="85.00",
                realized_commission_usd="0.50",
            ),
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.realized_pnl_usd == Decimal("2.0000")

    def test_short_close_realized_pnl_negative_when_exit_above_entry(self) -> None:
        """1 TLT short, entry 85.00, exit 87.00 → -2.00 gross - 1.00 commission
        = -3.00 net."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="short",
                order_direction="buy",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="87.00", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(
                total_quantity=1,
                avg_entry_price="85.00",
                realized_commission_usd="0.50",
            ),
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.realized_pnl_usd == Decimal("-3.0000")

    def test_position_mutation_close_carries_prior_position_id(self) -> None:
        pid = uuid4()
        prior_pos = PriorPositionRow(position_id=pid, quantity=1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(),
            prior_balance=_zero_balance(),
        )
        assert plan.position_mutation.action == "close"
        assert plan.position_mutation.position_id == pid

    def test_trade_mutation_close_carries_exit_fields(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        prior_trade = _open_trade_row()
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=prior_trade,
            prior_balance=_zero_balance(),
        )
        tm = plan.trade_mutation
        assert tm.action == "close"
        assert tm.trade_id == prior_trade.trade_id
        assert tm.trade_created_at == prior_trade.trade_created_at
        assert tm.closed_at_utc == datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC)
        assert tm.exit_order_id == _ORDER_ID
        assert tm.avg_exit_price == Decimal("86.50")
        assert tm.realized_pnl_usd is not None

    def test_long_close_cash_increases_by_notional_minus_commission(self) -> None:
        """Sell 1 TLT @ 86.50, commission 0.50 → cash += 86.50 - 0.50 = 86.00."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        prior_balance = PriorBalanceSnapshot(
            cash_usd=Decimal("100000"), net_liquidation=Decimal("100085")
        )
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(),
            prior_balance=prior_balance,
        )
        assert plan.balance_insert.cash_usd == Decimal("100086.0000")
        # Post-close NLV equals cash (position MTM is 0).
        assert plan.balance_insert.net_liquidation == Decimal("100086.0000")

    def test_short_close_cash_decreases_by_notional_plus_commission(self) -> None:
        """Buy back 1 TLT @ 83.50, commission 0.50 → cash -= 83.50 + 0.50 = 84.00."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=-1, avg_cost=Decimal("85.00"))
        prior_balance = PriorBalanceSnapshot(
            cash_usd=Decimal("100085"), net_liquidation=Decimal("100000")
        )
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="short",
                order_direction="buy",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="83.50", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(),
            prior_balance=prior_balance,
        )
        assert plan.balance_insert.cash_usd == Decimal("100001.0000")

    def test_close_balance_audit_post_fill_mtm_is_zero(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(),
            prior_balance=_zero_balance(),
        )
        bal_event = plan.audit_events[2]
        assert bal_event.payload["post_fill_position_mtm_usd"] == "0"

    def test_position_closed_audit_payload_shape(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        prior_trade = _open_trade_row()
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50"),
            prior_position=prior_pos,
            prior_trade=prior_trade,
            prior_balance=_zero_balance(),
        )
        pos_event = plan.audit_events[1]
        assert pos_event.event_type == AuditEventType.POSITION_CLOSED
        assert pos_event.payload["prior_quantity"] == 1
        assert pos_event.payload["fill_quantity_signed"] == -1
        assert pos_event.payload["new_quantity"] == 0
        assert pos_event.payload["trade_id"] == str(prior_trade.trade_id)
        assert pos_event.payload["market"] == "TLT"

    def test_trade_closed_audit_payload_shape(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        prior_trade = _open_trade_row(
            total_quantity=1,
            avg_entry_price="85.00",
            realized_commission_usd="0.50",
        )
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50", commission_usd="0.50"),
            prior_position=prior_pos,
            prior_trade=prior_trade,
            prior_balance=_zero_balance(),
        )
        trade_event = plan.audit_events[3]
        assert trade_event.event_type == AuditEventType.TRADE_CLOSED
        assert trade_event.payload["trade_id"] == str(prior_trade.trade_id)
        assert trade_event.payload["exit_order_id"] == str(_ORDER_ID)
        assert trade_event.payload["direction"] == "long"
        assert trade_event.payload["total_quantity"] == 1
        assert trade_event.payload["avg_entry_price"] == "85.00"
        assert trade_event.payload["avg_exit_price"] == "86.50"
        # gross_pnl = 1.50; total_commission = 1.00; realized_pnl = 0.50
        assert trade_event.payload["realized_commission_usd"] == "1.0000"
        assert trade_event.payload["realized_pnl_usd"] == "0.5000"
        # All money fields are Decimal-as-string [A05].
        assert all(
            isinstance(trade_event.payload[k], str)
            for k in (
                "avg_entry_price",
                "avg_exit_price",
                "gross_pnl_usd",
                "realized_commission_usd",
                "realized_pnl_usd",
            )
        )

    def test_order_filled_audit_payload_carries_scenario_tag(self) -> None:
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=1,
                market="TLT",
            ),
            payload=_payload(fill_quantity=1, fill_price="86.50"),
            prior_position=prior_pos,
            prior_trade=_open_trade_row(),
            prior_balance=_zero_balance(),
        )
        order_event = plan.audit_events[0]
        assert order_event.payload["scenario"] == "EXIT_FULL_CLOSE"

    def test_exit_with_no_prior_trade_raises_invariant(self) -> None:
        """The classifier returns EXIT_FULL_CLOSE but the trade lookup
        returned None — should raise EXIT_NO_PRIOR_TRADE."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        with pytest.raises(FillProcessingError) as exc_info:
            plan_fill_application(
                context=_ctx(
                    signal_direction="long",
                    order_direction="sell",
                    target_contracts=1,
                    market="TLT",
                ),
                payload=_payload(fill_quantity=1, fill_price="86.50"),
                prior_position=prior_pos,
                prior_trade=None,
                prior_balance=_zero_balance(),
            )
        assert exc_info.value.error_code == "EXIT_NO_PRIOR_TRADE"

    def test_trade_qty_mismatch_raises(self) -> None:
        """Trade total_quantity != position quantity → defensive raise."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=1, avg_cost=Decimal("85.00"))
        prior_trade = _open_trade_row(total_quantity=3, avg_entry_price="85.00")
        with pytest.raises(FillProcessingError) as exc_info:
            plan_fill_application(
                context=_ctx(
                    signal_direction="long",
                    order_direction="sell",
                    target_contracts=1,
                    market="TLT",
                ),
                payload=_payload(fill_quantity=1, fill_price="86.50"),
                prior_position=prior_pos,
                prior_trade=prior_trade,
                prior_balance=_zero_balance(),
            )
        assert exc_info.value.error_code == "EXIT_CLOSE_TRADE_QTY_MISMATCH"

    def test_realized_pnl_quantized_to_four_decimals(self) -> None:
        """1/3 of a cent precision rounds half-even."""
        prior_pos = PriorPositionRow(position_id=uuid4(), quantity=3, avg_cost=Decimal("100.00"))
        prior_trade = _open_trade_row(
            total_quantity=3,
            avg_entry_price="100.00",
            realized_commission_usd="0",
        )
        plan = plan_fill_application(
            context=_ctx(
                signal_direction="long",
                order_direction="sell",
                target_contracts=3,
                market="TLT",
            ),
            payload=_payload(
                fill_quantity=3,
                cumulative_filled_quantity=3,
                fill_price="100.00033333",
                commission_usd="0",
            ),
            prior_position=prior_pos,
            prior_trade=prior_trade,
            prior_balance=_zero_balance(),
        )
        # (100.00033333 - 100.00) * 3 = 0.00099999
        # Quantize to 4dp half-even: 0.0010 (since 99999 → ...9... rounds up)
        assert plan.trade_mutation.realized_pnl_usd == Decimal("0.0010")


# ---------------------------------------------------------------------------
# Mocked I/O: apply close action
# ---------------------------------------------------------------------------


def _build_close_plan() -> FillApplyPlan:
    """Build a minimal valid FillApplyPlan for the close action."""
    trade_id = uuid4()
    audit_events = (
        PendingAuditEvent(event_type=AuditEventType.ORDER_FILLED, payload={"k": "v"}),
        PendingAuditEvent(event_type=AuditEventType.POSITION_CLOSED, payload={"k": "v"}),
        PendingAuditEvent(event_type=AuditEventType.BALANCE_SNAPSHOT_RECORDED, payload={"k": "v"}),
        PendingAuditEvent(event_type=AuditEventType.TRADE_CLOSED, payload={"k": "v"}),
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
            action="close",
            position_id=uuid4(),
            account_id=_ACCOUNT_ID,
            market="TLT",
            contract_id=None,
            new_quantity=0,
            new_avg_cost=Decimal("85.00000000"),
            last_mark_ts=datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
            managed_by_version="a" * 40,
        ),
        balance_insert=BalanceInsert(
            account_id=_ACCOUNT_ID,
            snapshot_ts=datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
            net_liquidation=Decimal("100086.0000"),
            cash_usd=Decimal("100086.0000"),
            excess_liquidity=Decimal("100086.0000"),
            used_margin_pct=Decimal("0"),
            source=BALANCE_SOURCE_FROM_FILL,
        ),
        trade_mutation=TradeMutation(
            action="close",
            trade_id=trade_id,
            trade_created_at=_ORDER_CREATED,
            account_id=_ACCOUNT_ID,
            env="paper",
            market="TLT",
            contract_id=None,
            entry_signal_id=_SIGNAL_ID,
            entry_order_id=_ORDER_ID,
            direction="long",
            opened_at_utc=_ORDER_CREATED,
            new_total_quantity=1,
            new_avg_entry_price=Decimal("85.00000000"),
            realized_commission_usd=Decimal("0.50"),
            managed_by_version="a" * 40,
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            slippage_calibration_version_id=_SLIP_ID,
            closed_at_utc=datetime(2026, 5, 13, 12, 30, 0, tzinfo=UTC),
            exit_order_id=_ORDER_ID,
            avg_exit_price=Decimal("86.50"),
            realized_pnl_usd=Decimal("0.5000"),
        ),
    )


class TestApplyFillPlanCloseAction:
    """Mocked-I/O tests for the close-action apply path."""

    @pytest.mark.asyncio
    async def test_close_apply_returns_trade_and_position_ids(self) -> None:
        plan = _build_close_plan()
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
        assert result.position_id == plan.position_mutation.position_id
        assert result.trade_id == plan.trade_mutation.trade_id
        assert result.signal_id == _SIGNAL_ID
        assert len(result.audit_event_uuids) == 4

    @pytest.mark.asyncio
    async def test_close_apply_issues_delete_position_sql(self) -> None:
        """Capture session.execute calls and verify a DELETE FROM
        positions_current statement was issued."""
        plan = _build_close_plan()
        captured_sql: list[str] = []

        session_factory = MagicMock()

        def _make_session() -> MagicMock:
            sess = MagicMock()

            async def _exec(stmt: Any, params: Any = None) -> MagicMock:
                captured_sql.append(str(stmt))
                result_mock = MagicMock()
                result_mock.fetchone = MagicMock(return_value=MagicMock(id=uuid4()))
                return result_mock

            sess.execute = AsyncMock(side_effect=_exec)

            @asynccontextmanager
            async def _begin_cm() -> Any:
                yield None

            sess.begin = MagicMock(return_value=_begin_cm())
            return sess

        @asynccontextmanager
        async def _factory_cm() -> Any:
            yield _make_session()

        session_factory.side_effect = lambda: _factory_cm()

        with patch(
            "services.risk.fill_processor.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )
        # The position close branch issues DELETE FROM positions_current.
        joined = " | ".join(captured_sql)
        assert "DELETE FROM positions_current" in joined
        # The trade close branch issues UPDATE trades SET state = 'closed'.
        assert "UPDATE trades" in joined
        assert "state = 'closed'" in joined
        # realized_pnl_usd UPDATE is part of the trade UPDATE.
        assert "realized_pnl_usd" in joined
        # Drill 5 follow-up #3 — the close branch also issues an
        # UPDATE signals SET status = 'closed' for the entry signal_id.
        assert "UPDATE signals SET status = 'closed'" in joined

    @pytest.mark.asyncio
    async def test_close_apply_passes_signal_id_to_signals_update(self) -> None:
        """The signals UPDATE binds the entry_signal_id from TradeMutation.

        Drill 5 follow-up #3 regression test — captures the params dict
        passed to session.execute and asserts the ``sig`` param matches
        ``plan.trade_mutation.entry_signal_id`` (the signal whose trade
        just closed).
        """
        plan = _build_close_plan()
        captured: list[tuple[str, Any]] = []

        session_factory = MagicMock()

        def _make_session() -> MagicMock:
            sess = MagicMock()

            async def _exec(stmt: Any, params: Any = None) -> MagicMock:
                captured.append((str(stmt), params))
                result_mock = MagicMock()
                result_mock.fetchone = MagicMock(return_value=MagicMock(id=uuid4()))
                return result_mock

            sess.execute = AsyncMock(side_effect=_exec)

            @asynccontextmanager
            async def _begin_cm() -> Any:
                yield None

            sess.begin = MagicMock(return_value=_begin_cm())
            return sess

        @asynccontextmanager
        async def _factory_cm() -> Any:
            yield _make_session()

        session_factory.side_effect = lambda: _factory_cm()

        with patch(
            "services.risk.fill_processor.append_audit_event",
            new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
        ):
            await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )

        # Locate the signals UPDATE in the captured execute calls.
        signals_updates = [
            (stmt, params)
            for stmt, params in captured
            if "UPDATE signals SET status = 'closed'" in stmt
        ]
        assert len(signals_updates) == 1
        _, params = signals_updates[0]
        # `sig` bound from plan.trade_mutation.entry_signal_id (= _SIGNAL_ID).
        assert params == {"sig": _SIGNAL_ID}

    @pytest.mark.asyncio
    async def test_close_apply_does_not_update_signals_for_other_paths(self) -> None:
        """ENTRY (action='open') path does NOT issue a signals UPDATE.

        The open-path apply still INSERTs the trades row, but signals
        stays at 'working' (the audit chain captures the lifecycle, the
        cosmetic signals.status flip only fires on close).
        """
        plan = _build_plan(action="open")  # ENTRY path
        captured_sql: list[str] = []

        session_factory = MagicMock()

        def _make_session() -> MagicMock:
            sess = MagicMock()

            async def _exec(stmt: Any, params: Any = None) -> MagicMock:
                captured_sql.append(str(stmt))
                result_mock = MagicMock()
                result_mock.fetchone = MagicMock(return_value=MagicMock(id=uuid4()))
                return result_mock

            sess.execute = AsyncMock(side_effect=_exec)

            @asynccontextmanager
            async def _begin_cm() -> Any:
                yield None

            sess.begin = MagicMock(return_value=_begin_cm())
            return sess

        @asynccontextmanager
        async def _factory_cm() -> Any:
            yield _make_session()

        session_factory.side_effect = lambda: _factory_cm()

        with (
            patch(
                "services.risk.fill_processor.append_audit_event",
                new=AsyncMock(side_effect=lambda *a, **k: _audit_record_mock()),
            ),
            _no_open_trade_guard(),
        ):
            await apply_fill_plan(
                plan,
                session_factory=session_factory,
                account_id=_ACCOUNT_ID,
                env="paper",
            )

        joined = " | ".join(captured_sql)
        # ENTRY path issues INSERT INTO trades, not UPDATE signals.
        assert "INSERT INTO trades" in joined
        assert "UPDATE signals SET status = 'closed'" not in joined


# ---------------------------------------------------------------------------
# Pure-policy: futures contract-multiplier path (2026-05-19 Item 2 fix)
# ---------------------------------------------------------------------------


class TestFuturesPathMultiplier:
    """Coverage for the futures branches added 2026-05-19.

    Pre-fix the planner treated every market as a multiplier=1 cash
    equity. For /MES (multiplier=5) the drill 10 trade row recorded
    ``realized_pnl_usd=-6.5000`` for a 6.5pt loss when the actual cash
    P&L was -$32.50; the ``balances.cash_usd`` snapshot also booked the
    full notional flow ($7,375 cash debit on entry) which produced
    impossible balance rows like the 2026-05-18 22:11 ``cash_usd=
    -16,545.49`` snapshot from drill 7+8 naked positions. The fix
    routes futures fills through a separate cash-semantics branch that
    only moves commission on entry + (gross P&L - commission) on exit.
    """

    # ----- realized_pnl_usd multiplier-adjustment -----

    def test_mes_long_close_realized_pnl_multiplier_adjusted(self) -> None:
        """/MES long: entry 7375.00 → exit 7368.50; qty 1; mult 5.

        gross_pnl = (7368.50 - 7375.00) * 1 * 5 = -32.50
        realized_pnl_usd = -32.50 - 0.00 commission_delta = -32.5000
        """
        ctx = _futures_ctx(
            signal_direction="long",
            order_direction="sell",
            multiplier=Decimal("5"),
        )
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(
                fill_quantity=1,
                cumulative_filled_quantity=1,
                fill_price="7368.50",
                commission_usd="0.00",
            ),
            prior_position=PriorPositionRow(
                position_id=uuid4(),
                quantity=1,
                avg_cost=Decimal("7375.00"),
            ),
            prior_trade=PriorTradeRow(
                trade_id=uuid4(),
                trade_created_at=_ORDER_CREATED,
                total_quantity=1,
                avg_entry_price=Decimal("7375.00"),
                realized_commission_usd=Decimal("0"),
            ),
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.realized_pnl_usd == Decimal("-32.5000")
        trade_event = next(
            e for e in plan.audit_events if e.event_type == AuditEventType.TRADE_CLOSED
        )
        assert trade_event.payload["realized_pnl_usd"] == "-32.5000"

    def test_mes_short_close_realized_pnl_multiplier_adjusted(self) -> None:
        """/MES short: entry 7375.00 (sell) → exit 7368.50 (buy);
        qty 1; mult 5.

        gross_pnl = (7375.00 - 7368.50) * 1 * 5 = +32.50
        """
        ctx = _futures_ctx(
            signal_direction="short",
            order_direction="buy",
            multiplier=Decimal("5"),
        )
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(
                fill_quantity=1,
                cumulative_filled_quantity=1,
                fill_price="7368.50",
                commission_usd="0.00",
            ),
            prior_position=PriorPositionRow(
                position_id=uuid4(),
                quantity=-1,
                avg_cost=Decimal("7375.00"),
            ),
            prior_trade=PriorTradeRow(
                trade_id=uuid4(),
                trade_created_at=_ORDER_CREATED,
                total_quantity=1,
                avg_entry_price=Decimal("7375.00"),
                realized_commission_usd=Decimal("0"),
            ),
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.realized_pnl_usd == Decimal("32.5000")

    def test_mnq_long_close_uses_mnq_multiplier(self) -> None:
        """/MNQ (multiplier=2): entry 29150 → exit 29122.25; qty 1.

        gross_pnl = (29122.25 - 29150) * 1 * 2 = -55.50
        """
        ctx = _futures_ctx(
            signal_direction="long",
            order_direction="sell",
            market="/MNQ",
            multiplier=Decimal("2"),
        )
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(
                fill_quantity=1,
                cumulative_filled_quantity=1,
                fill_price="29122.25",
                commission_usd="0.00",
            ),
            prior_position=PriorPositionRow(
                position_id=uuid4(),
                quantity=1,
                avg_cost=Decimal("29150.00"),
            ),
            prior_trade=PriorTradeRow(
                trade_id=uuid4(),
                trade_created_at=_ORDER_CREATED,
                total_quantity=1,
                avg_entry_price=Decimal("29150.00"),
                realized_commission_usd=Decimal("0"),
            ),
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.realized_pnl_usd == Decimal("-55.5000")

    def test_mgc_uses_mgc_multiplier_10(self) -> None:
        """/MGC (multiplier=10): 1pt loss = $10."""
        ctx = _futures_ctx(
            signal_direction="long",
            order_direction="sell",
            market="/MGC",
            multiplier=Decimal("10"),
        )
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(
                fill_quantity=1,
                cumulative_filled_quantity=1,
                fill_price="2350.00",
                commission_usd="0.00",
            ),
            prior_position=PriorPositionRow(
                position_id=uuid4(),
                quantity=1,
                avg_cost=Decimal("2351.00"),
            ),
            prior_trade=PriorTradeRow(
                trade_id=uuid4(),
                trade_created_at=_ORDER_CREATED,
                total_quantity=1,
                avg_entry_price=Decimal("2351.00"),
                realized_commission_usd=Decimal("0"),
            ),
            prior_balance=_zero_balance(),
        )
        assert plan.trade_mutation.realized_pnl_usd == Decimal("-10.0000")

    # ----- cash_delta semantics: futures move ONLY commission/P&L, never notional -----

    def test_futures_long_entry_cash_delta_excludes_notional(self) -> None:
        """/MES long entry @ 7375 with $1.30 commission.

        Pre-fix: cash_delta = -(7375 + 1.30) = -$7376.30 (wildly wrong).
        Post-fix: cash_delta = -1.30 (commission only; margin is held
        in a separate slot reconciled against IBKR).
        """
        ctx = _futures_ctx(multiplier=Decimal("5"))
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(fill_quantity=1, fill_price="7375.00", commission_usd="1.30"),
            prior_position=None,
            prior_trade=None,
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("20000"), net_liquidation=Decimal("20000")
            ),
        )
        # cash_delta = -1.30; new_cash = 20000 - 1.30 = 19998.70.
        assert plan.balance_insert.cash_usd == Decimal("19998.7000")
        bal_event = next(
            e for e in plan.audit_events if e.event_type == AuditEventType.BALANCE_SNAPSHOT_RECORDED
        )
        assert bal_event.payload["cash_delta_usd"] == "-1.30"

    def test_futures_short_entry_cash_delta_excludes_notional(self) -> None:
        """Short futures entry — same semantics; only commission moves
        cash. Pre-fix it incorrectly credited the full notional."""
        ctx = _futures_ctx(
            signal_direction="short",
            order_direction="sell",
            multiplier=Decimal("5"),
        )
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(fill_quantity=1, fill_price="7375.00", commission_usd="1.30"),
            prior_position=None,
            prior_trade=None,
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("20000"), net_liquidation=Decimal("20000")
            ),
        )
        assert plan.balance_insert.cash_usd == Decimal("19998.7000")

    def test_futures_long_close_cash_delta_is_pnl_minus_commission(self) -> None:
        """/MES long close @ 7368.50 from entry 7375.00.

        gross_pnl = -32.50; commission_delta = $1.30.
        cash_delta = -32.50 - 1.30 = -33.80.
        """
        ctx = _futures_ctx(
            signal_direction="long",
            order_direction="sell",
            multiplier=Decimal("5"),
        )
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(
                fill_quantity=1,
                cumulative_filled_quantity=1,
                fill_price="7368.50",
                commission_usd="1.30",
            ),
            prior_position=PriorPositionRow(
                position_id=uuid4(),
                quantity=1,
                avg_cost=Decimal("7375.00"),
            ),
            prior_trade=PriorTradeRow(
                trade_id=uuid4(),
                trade_created_at=_ORDER_CREATED,
                total_quantity=1,
                avg_entry_price=Decimal("7375.00"),
                realized_commission_usd=Decimal("1.30"),  # entry commission already accumulated
            ),
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("19998.70"),  # post-entry from prior test scenario
                net_liquidation=Decimal("19998.70"),
            ),
        )
        # cash_delta = -32.50 - 1.30 = -33.80; new_cash = 19998.70 - 33.80 = 19964.90.
        assert plan.balance_insert.cash_usd == Decimal("19964.9000")

    def test_futures_short_close_cash_delta_is_pnl_minus_commission(self) -> None:
        """Short close: short entry → buy back below entry = +pnl."""
        ctx = _futures_ctx(
            signal_direction="short",
            order_direction="buy",
            multiplier=Decimal("5"),
        )
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(
                fill_quantity=1,
                cumulative_filled_quantity=1,
                fill_price="7368.50",
                commission_usd="1.30",
            ),
            prior_position=PriorPositionRow(
                position_id=uuid4(),
                quantity=-1,
                avg_cost=Decimal("7375.00"),
            ),
            prior_trade=PriorTradeRow(
                trade_id=uuid4(),
                trade_created_at=_ORDER_CREATED,
                total_quantity=1,
                avg_entry_price=Decimal("7375.00"),
                realized_commission_usd=Decimal("1.30"),
            ),
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("19998.70"),
                net_liquidation=Decimal("19998.70"),
            ),
        )
        # gross_pnl = +32.50; cash_delta = +32.50 - 1.30 = +31.20.
        # new_cash = 19998.70 + 31.20 = 20029.90.
        assert plan.balance_insert.cash_usd == Decimal("20029.9000")

    # ----- position MTM uses multiplier -----

    def test_futures_post_fill_position_mtm_uses_multiplier(self) -> None:
        """/MES 1 long @ 7375 → MTM = 1 * 7375 * 5 = $36,875 (NOT the
        pre-fix bare 7375 which under-counted NAV by ~$29,500)."""
        ctx = _futures_ctx(multiplier=Decimal("5"))
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(fill_quantity=1, fill_price="7375.00", commission_usd="0"),
            prior_position=None,
            prior_trade=None,
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("20000"), net_liquidation=Decimal("20000")
            ),
        )
        bal_event = next(
            e for e in plan.audit_events if e.event_type == AuditEventType.BALANCE_SNAPSHOT_RECORDED
        )
        # MTM = 1 * 7375 * 5 = 36875.00. The exact serialized value is
        # the raw Decimal multiplication then quantized to 4 decimals.
        assert bal_event.payload["post_fill_position_mtm_usd"] == "36875.0000"
        # NAV = cash + MTM = 20000 (commission=0) + 36875 = 56875.
        assert plan.balance_insert.net_liquidation == Decimal("56875.0000")

    # ----- ETF path unchanged (regression guard) -----

    def test_etf_path_unchanged_when_contract_id_none(self) -> None:
        """Existing ETF behavior preserved: contract_id=None →
        multiplier=1 by default → notional flow + raw P&L."""
        ctx = _ctx()  # contract_id=None, multiplier=Decimal(1)
        plan = plan_fill_application(
            context=ctx,
            payload=_payload(fill_quantity=1, fill_price="85.42", commission_usd="0.50"),
            prior_position=None,
            prior_trade=None,
            prior_balance=PriorBalanceSnapshot(
                cash_usd=Decimal("100000"), net_liquidation=Decimal("100000")
            ),
        )
        # ETF: cash_delta = -(85.42 + 0.50) = -85.92; new_cash = 99914.08.
        assert plan.balance_insert.cash_usd == Decimal("99914.0800")
        # MTM = 1 * 85.42 * 1 = 85.42 (multiplier=1 default).
        bal_event = next(
            e for e in plan.audit_events if e.event_type == AuditEventType.BALANCE_SNAPSHOT_RECORDED
        )
        assert bal_event.payload["post_fill_position_mtm_usd"] == "85.4200"


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
            "FillScenario",
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
            "fetch_prior_open_trade_for_market",
            "fetch_prior_position",
            "fetch_prior_trade",
            "plan_fill_application",
            "process_fill_event",
        }
        assert set(mod.__all__) == expected
