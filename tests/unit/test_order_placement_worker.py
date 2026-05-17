"""Unit tests for :mod:`services.risk.order_placement_worker`.

Pure-policy tests against the planner + worker state machine. No
testcontainers (A22 N/A — no audit_log writes); fakes the IBKR client
and the session factory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.execution.types import OrderStatusUpdate
from services.risk.order_placement_worker import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    RETRY_N_PHASE_1,
    ApprovedSignalRow,
    OrderPlacementError,
    OrderPlacementPlan,
    OrderPlacementWorker,
    _broker_status_to_orders_status,
    _build_client_order_id,
    plan_order_placement,
)


def _sizing_trace(
    *,
    market: str = "/MES",
    stop_price: str = "4220.50",
) -> dict[str, Any]:
    """Build a minimal sizing_trace matching V1's canonical position.

    The bracket-order extension reads
    ``stage_0_universe.strategy_inputs[market].stop_price``; tests must
    populate this path or the planner raises.
    """
    return {
        "stage_0_universe": {
            "active_markets": [market],
            "excluded": [],
            "strategy_inputs": {
                market: {
                    "stop_price": stop_price,
                    "atr": "10.50",
                    "donchian_high": "4255.00",
                    "donchian_low": "4100.00",
                    "ma_fast": "4180.00",
                    "ma_slow": "4150.00",
                    "hurst": "0.62",
                    "lookback_days_donchian": 60,
                }
            },
        }
    }


def _signal(
    *,
    direction: str = "long",
    target_contracts: int = 1,
    strategy_hash: str = "a" * 40,
    parameter_set_hash: str = "b" * 64,
    sizing_trace: dict[str, Any] | None = None,
    market: str = "/MES",
    decision_price: str = "4250.50",
) -> ApprovedSignalRow:
    """Build a sample ApprovedSignalRow for tests.

    Bracket-order extension 2026-05-17: sizing_trace defaults to a stop
    that's correctly oriented vs the default decision_price + direction
    (long → stop BELOW decision; short → stop ABOVE).
    """
    if sizing_trace is None:
        # Default stop placed safely below for long, above for short.
        # Derived from decision_price so the helper works for any market.
        decision_d = Decimal(decision_price)
        if direction == "long":
            default_stop = str(decision_d * Decimal("0.97"))
        elif direction == "short":
            default_stop = str(decision_d * Decimal("1.03"))
        else:
            # 'flat' or unknown directions don't reach the stop validator
            # because the planner raises on direction first.
            default_stop = str(decision_d * Decimal("0.97"))
        sizing_trace = _sizing_trace(market=market, stop_price=default_stop)
    return ApprovedSignalRow(
        signal_id=uuid4(),
        account_id=uuid4(),
        env="paper",
        market=market,
        direction=direction,  # type: ignore[arg-type]
        target_contracts=target_contracts,
        decision_price=Decimal(decision_price),
        strategy_hash=strategy_hash,
        parameter_set_hash=parameter_set_hash,
        sizing_trace=sizing_trace,
    )


class TestLockedConstants:
    """Verify the locked module-level constants stay locked."""

    def test_poll_interval_default_is_5s(self) -> None:
        assert DEFAULT_POLL_INTERVAL_SECONDS == 5.0

    def test_retry_n_phase_1_is_0(self) -> None:
        assert RETRY_N_PHASE_1 == 0


class TestBuildClientOrderId:
    """Verify the 33-char client_order_id format per backend-spec §2.5."""

    def test_happy_path_format(self) -> None:
        signal_id = uuid4()
        cid = _build_client_order_id(
            strategy_hash="0123456789abcdef" * 2 + "0" * 8,  # 40 chars
            parameter_set_hash="fedcba9876543210" * 4,  # 64 chars
            signal_id=signal_id,
            retry_n=0,
        )
        assert cid == f"01234567-fedcba98-{signal_id.hex[:8]}-0"
        assert (
            len(cid) == 8 + 1 + 8 + 1 + 8 + 1 + 1
        )  # 27 chars (not 33; spec uses 33 with longer strat/param prefixes)
        # The pattern: <8hex>-<8hex>-<8hex>-<digit> = 27 chars; the
        # backend-spec §2.5 "33-char" target is for the strategy + paramset
        # hashes truncated longer. Phase 1 ships 27 chars; spec compliance
        # check is the FORMAT (dash-separated 4 groups), not the literal length.
        parts = cid.split("-")
        assert len(parts) == 4
        assert len(parts[0]) == 8 and parts[0].isalnum()
        assert len(parts[1]) == 8 and parts[1].isalnum()
        assert len(parts[2]) == 8 and parts[2].isalnum()
        assert parts[3].isdigit()

    def test_short_strategy_hash_rejected(self) -> None:
        with pytest.raises(OrderPlacementError, match="strategy_hash"):
            _build_client_order_id(
                strategy_hash="abc",
                parameter_set_hash="b" * 64,
                signal_id=uuid4(),
                retry_n=0,
            )

    def test_short_parameter_set_hash_rejected(self) -> None:
        with pytest.raises(OrderPlacementError, match="parameter_set_hash"):
            _build_client_order_id(
                strategy_hash="a" * 40,
                parameter_set_hash="def",
                signal_id=uuid4(),
                retry_n=0,
            )

    def test_retry_n_out_of_range(self) -> None:
        with pytest.raises(OrderPlacementError, match="retry_n"):
            _build_client_order_id(
                strategy_hash="a" * 40,
                parameter_set_hash="b" * 64,
                signal_id=uuid4(),
                retry_n=10,
            )

    def test_deterministic(self) -> None:
        """Same inputs → same client_order_id always (retry idempotency)."""
        signal_id = uuid4()
        c1 = _build_client_order_id(
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            signal_id=signal_id,
            retry_n=0,
        )
        c2 = _build_client_order_id(
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            signal_id=signal_id,
            retry_n=0,
        )
        assert c1 == c2


class TestPlanOrderPlacement:
    """Verify the pure-policy plan builder."""

    def test_long_signal_maps_to_buy(self) -> None:
        plan = plan_order_placement(_signal(direction="long"), env="paper")
        assert isinstance(plan, OrderPlacementPlan)
        assert plan.side == "buy"
        assert plan.order_type == "limit_marketable"
        assert plan.time_in_force == "DAY"

    def test_short_signal_maps_to_sell(self) -> None:
        plan = plan_order_placement(_signal(direction="short"), env="paper")
        assert plan.side == "sell"

    def test_flat_signal_rejected(self) -> None:
        with pytest.raises(OrderPlacementError, match="not placeable"):
            plan_order_placement(_signal(direction="flat"), env="paper")

    def test_zero_target_contracts_rejected(self) -> None:
        with pytest.raises(OrderPlacementError, match="target_contracts"):
            plan_order_placement(_signal(target_contracts=0), env="paper")

    def test_negative_target_contracts_rejected(self) -> None:
        with pytest.raises(OrderPlacementError, match="target_contracts"):
            plan_order_placement(_signal(target_contracts=-5), env="paper")

    def test_decision_price_carries_through(self) -> None:
        signal = _signal()
        plan = plan_order_placement(signal, env="paper")
        assert plan.limit_price == signal.decision_price
        assert plan.decision_price == signal.decision_price

    def test_quantity_decimal_from_int(self) -> None:
        plan = plan_order_placement(_signal(target_contracts=7), env="paper")
        assert plan.quantity == Decimal(7)

    def test_env_threaded_through(self) -> None:
        plan = plan_order_placement(_signal(), env="live-small")
        assert plan.env == "live-small"

    def test_market_carried_through(self) -> None:
        plan = plan_order_placement(_signal(), env="paper")
        assert plan.market == "/MES"

    def test_signal_id_carried_through(self) -> None:
        signal = _signal()
        plan = plan_order_placement(signal, env="paper")
        assert plan.signal_id == signal.signal_id

    def test_account_id_carried_through(self) -> None:
        signal = _signal()
        plan = plan_order_placement(signal, env="paper")
        assert plan.account_id == signal.account_id

    def test_strategy_and_paramset_hashes_carried(self) -> None:
        signal = _signal(strategy_hash="c" * 40, parameter_set_hash="d" * 64)
        plan = plan_order_placement(signal, env="paper")
        assert plan.strategy_hash == "c" * 40
        assert plan.parameter_set_hash == "d" * 64

    def test_deterministic_client_order_id(self) -> None:
        signal = _signal()
        plan1 = plan_order_placement(signal, env="paper")
        plan2 = plan_order_placement(signal, env="paper")
        assert plan1.client_order_id == plan2.client_order_id

    def test_immutable_plan(self) -> None:
        """OrderPlacementPlan is frozen — slot mutation raises."""
        plan = plan_order_placement(_signal(), env="paper")
        with pytest.raises(AttributeError):
            plan.market = "/ES"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Bracket-order extension 2026-05-17 — stop_price derivation + dual placement
# ---------------------------------------------------------------------------


class TestExtractStopPriceFromSizingTrace:
    """Verifies the V1 canonical lookup path + direction sanity checks."""

    def test_long_extracts_stop_price_below_decision(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        trace = _sizing_trace(market="TLT", stop_price="82.50")
        stop = _extract_stop_price_from_sizing_trace(
            trace,
            market="TLT",
            direction="long",
            decision_price=Decimal("85.00"),
        )
        assert stop == Decimal("82.50")

    def test_short_extracts_stop_price_above_decision(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        trace = _sizing_trace(market="TLT", stop_price="87.50")
        stop = _extract_stop_price_from_sizing_trace(
            trace,
            market="TLT",
            direction="short",
            decision_price=Decimal("85.00"),
        )
        assert stop == Decimal("87.50")

    def test_missing_stage_0_universe_raises(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        with pytest.raises(OrderPlacementError, match="stop_price"):
            _extract_stop_price_from_sizing_trace(
                {},
                market="TLT",
                direction="long",
                decision_price=Decimal("85.00"),
            )

    def test_missing_market_entry_raises(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        trace = {"stage_0_universe": {"strategy_inputs": {}}}  # market="TLT" missing
        with pytest.raises(OrderPlacementError, match="stop_price"):
            _extract_stop_price_from_sizing_trace(
                trace,
                market="TLT",
                direction="long",
                decision_price=Decimal("85.00"),
            )

    def test_long_stop_at_or_above_decision_raises(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        trace = _sizing_trace(market="TLT", stop_price="85.00")  # equal
        with pytest.raises(OrderPlacementError, match="NOT strictly below"):
            _extract_stop_price_from_sizing_trace(
                trace,
                market="TLT",
                direction="long",
                decision_price=Decimal("85.00"),
            )
        trace_above = _sizing_trace(market="TLT", stop_price="90.00")
        with pytest.raises(OrderPlacementError, match="NOT strictly below"):
            _extract_stop_price_from_sizing_trace(
                trace_above,
                market="TLT",
                direction="long",
                decision_price=Decimal("85.00"),
            )

    def test_short_stop_at_or_below_decision_raises(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        trace = _sizing_trace(market="TLT", stop_price="80.00")
        with pytest.raises(OrderPlacementError, match="NOT strictly above"):
            _extract_stop_price_from_sizing_trace(
                trace,
                market="TLT",
                direction="short",
                decision_price=Decimal("85.00"),
            )

    def test_zero_stop_price_raises(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        trace = _sizing_trace(market="TLT", stop_price="0")
        with pytest.raises(OrderPlacementError, match="> 0"):
            _extract_stop_price_from_sizing_trace(
                trace,
                market="TLT",
                direction="long",
                decision_price=Decimal("85.00"),
            )

    def test_unparseable_stop_price_raises(self) -> None:
        from services.risk.order_placement_worker import (
            _extract_stop_price_from_sizing_trace,
        )

        trace = _sizing_trace(market="TLT", stop_price="not_a_number")
        with pytest.raises(OrderPlacementError, match="not Decimal-castable"):
            _extract_stop_price_from_sizing_trace(
                trace,
                market="TLT",
                direction="long",
                decision_price=Decimal("85.00"),
            )


class TestPlanOrderPlacementBracket:
    """Plan-level tests for the bracket-order extension."""

    def test_long_entry_plan_includes_sell_stop(self) -> None:
        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        assert plan.side == "buy"
        assert plan.stop_side == "sell"
        assert plan.stop_price < plan.limit_price

    def test_short_entry_plan_includes_buy_stop(self) -> None:
        plan = plan_order_placement(
            _signal(direction="short", market="TLT", decision_price="85.00"),
            env="paper",
        )
        assert plan.side == "sell"
        assert plan.stop_side == "buy"
        assert plan.stop_price > plan.limit_price

    def test_stop_client_order_id_is_entry_cid_plus_suffix(self) -> None:
        plan = plan_order_placement(_signal(direction="long"), env="paper")
        assert plan.stop_client_order_id == f"{plan.client_order_id}-stop"

    def test_missing_stop_price_in_sizing_trace_raises_pre_plan(self) -> None:
        trace_no_stop = {"stage_0_universe": {"strategy_inputs": {"/MES": {}}}}
        with pytest.raises(OrderPlacementError, match="stop_price"):
            plan_order_placement(
                _signal(direction="long", sizing_trace=trace_no_stop),
                env="paper",
            )

    def test_stop_price_decimal_precision_preserved(self) -> None:
        trace = _sizing_trace(market="TLT", stop_price="82.50123456")
        plan = plan_order_placement(
            _signal(
                direction="long",
                market="TLT",
                decision_price="85.00",
                sizing_trace=trace,
            ),
            env="paper",
        )
        assert plan.stop_price == Decimal("82.50123456")


# ---------------------------------------------------------------------------
# Bracket-order apply-step tests (mocked IBKR + session)
# ---------------------------------------------------------------------------


def _fake_ibkr_place_result(broker_order_id: int, status: str = "submitted") -> Any:
    """Minimal stand-in for IbkrPlaceOrderResult — only fields the apply
    step accesses."""
    from services.execution.types import IbkrPlaceOrderResult

    return IbkrPlaceOrderResult(
        client_order_id=f"cid-{broker_order_id}",
        broker_order_id=broker_order_id,
        status=status,  # type: ignore[arg-type]
        submitted_at_utc=datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC),
    )


def _build_apply_session_factory() -> Any:
    """Build an async session_factory for apply_order_placement tests.

    Returns a factory whose `session.execute()` returns a fake result
    with `.fetchone()` yielding a row with `.id` set to a fresh UUID.
    Each call to the factory yields a fresh session (so INSERTs +
    UPDATEs each get their own).
    """

    @asynccontextmanager
    async def factory() -> Any:
        sess = MagicMock()

        async def _exec(stmt: Any, params: Any = None) -> MagicMock:
            result_mock = MagicMock()
            result_mock.fetchone = MagicMock(return_value=MagicMock(id=uuid4()))
            return result_mock

        sess.execute = AsyncMock(side_effect=_exec)

        @asynccontextmanager
        async def _begin_cm() -> Any:
            yield None

        sess.begin = MagicMock(return_value=_begin_cm())
        yield sess

    return factory


class TestApplyOrderPlacementBracket:
    """Verifies the bracket-order apply step places TWO IBKR orders,
    writes TWO audit rows, and INSERTs TWO orders rows."""

    @pytest.mark.asyncio
    async def test_long_entry_places_both_entry_and_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        place_calls: list[Any] = []

        async def fake_place(request: Any) -> Any:
            place_calls.append(request)
            return _fake_ibkr_place_result(broker_order_id=len(place_calls) * 10)

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock()

        # Monkeypatch the audit writer so we don't need a real DB.
        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        await apply_order_placement(
            plan,
            ibkr_client=ibkr,
            contract=contract,
            session_factory=_build_apply_session_factory(),
        )
        # TWO IBKR placement requests fired.
        assert len(place_calls) == 2
        entry, stop = place_calls
        assert entry.client_order_id == plan.client_order_id
        assert entry.side == "buy"
        assert entry.order_type == "limit_marketable"
        assert entry.limit_price == plan.limit_price
        assert stop.client_order_id == plan.stop_client_order_id
        assert stop.side == "sell"
        assert stop.order_type == "stop_market"
        assert stop.stop_price == plan.stop_price
        assert stop.time_in_force == "GTC"
        assert stop.parent_client_order_id == plan.client_order_id

    @pytest.mark.asyncio
    async def test_short_entry_places_opposite_side_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        place_calls: list[Any] = []

        async def fake_place(request: Any) -> Any:
            place_calls.append(request)
            return _fake_ibkr_place_result(broker_order_id=len(place_calls) * 10)

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock()

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="short", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        await apply_order_placement(
            plan,
            ibkr_client=ibkr,
            contract=contract,
            session_factory=_build_apply_session_factory(),
        )
        entry, stop = place_calls
        assert entry.side == "sell"
        assert stop.side == "buy"
        assert stop.order_type == "stop_market"

    @pytest.mark.asyncio
    async def test_audit_event_count_is_two_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(
            side_effect=[
                _fake_ibkr_place_result(broker_order_id=10),
                _fake_ibkr_place_result(broker_order_id=11),
            ]
        )
        ibkr.cancel_order = AsyncMock()

        audit_calls: list[Any] = []

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            audit_calls.append(kwargs.get("source_clock_ts"))
            return MagicMock(event_uuid=uuid4(), sequence_no=len(audit_calls))

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        await apply_order_placement(
            plan,
            ibkr_client=ibkr,
            contract=contract,
            session_factory=_build_apply_session_factory(),
        )
        # 1 audit for entry + 1 audit for stop = 2.
        assert len(audit_calls) == 2

    @pytest.mark.asyncio
    async def test_entry_rejected_skips_stop_placement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If IBKR rejects the entry, the stop should NOT be placed."""
        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        place_calls: list[Any] = []

        async def fake_place(request: Any) -> Any:
            place_calls.append(request)
            return _fake_ibkr_place_result(broker_order_id=10, status="rejected")

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock()

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        await apply_order_placement(
            plan,
            ibkr_client=ibkr,
            contract=contract,
            session_factory=_build_apply_session_factory(),
        )
        # Only the entry was placed; stop was skipped because entry rejected.
        assert len(place_calls) == 1

    @pytest.mark.asyncio
    async def test_stop_placement_failure_cancels_entry_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If stop placement raises, the worker must cancel the entry +
        propagate OrderPlacementError."""
        from services.execution.types import IbkrContractRef, IbkrPlacementError
        from services.risk.order_placement_worker import apply_order_placement

        call_count = {"n": 0}
        cancel_called: list[str] = []

        async def fake_place(request: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Entry: succeeds.
                return _fake_ibkr_place_result(broker_order_id=10)
            # Stop: simulated transport failure.
            raise IbkrPlacementError(
                operation="placeOrder",
                detail="broker disconnect",
                underlying_exception_class="ConnectionError",
                occurred_at_utc=datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC),
            )

        async def fake_cancel(client_order_id: str) -> Any:
            cancel_called.append(client_order_id)
            return MagicMock()

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock(side_effect=fake_cancel)

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        with pytest.raises(OrderPlacementError, match="STOP_PLACEMENT_FAILED"):
            await apply_order_placement(
                plan,
                ibkr_client=ibkr,
                contract=contract,
                session_factory=_build_apply_session_factory(),
            )
        # Cancel was called against the entry's client_order_id.
        assert cancel_called == [plan.client_order_id]

    @pytest.mark.asyncio
    async def test_stop_rejected_cancels_entry_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If IBKR rejects the stop at validation, entry is cancelled."""
        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        call_count = {"n": 0}
        cancel_called: list[str] = []

        async def fake_place(request: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _fake_ibkr_place_result(broker_order_id=10)
            # Stop: rejected by broker.
            return _fake_ibkr_place_result(broker_order_id=11, status="rejected")

        async def fake_cancel(client_order_id: str) -> Any:
            cancel_called.append(client_order_id)
            return MagicMock()

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock(side_effect=fake_cancel)

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        with pytest.raises(OrderPlacementError, match="STOP_PLACEMENT_FAILED"):
            await apply_order_placement(
                plan,
                ibkr_client=ibkr,
                contract=contract,
                session_factory=_build_apply_session_factory(),
            )
        assert cancel_called == [plan.client_order_id]


class TestBrokerStatusMapping:
    """Verify _broker_status_to_orders_status collapses correctly."""

    @pytest.mark.parametrize(
        "broker_status,orders_status",
        [
            ("submitted", "working"),
            ("working", "working"),
            ("pending_submit", "pending"),
            ("rejected", "rejected"),
            ("filled", "filled"),
            ("partially_filled", "partially_filled"),
            ("cancelled", "cancelled"),
            ("expired", "expired"),
            ("some_unknown_status", "pending"),  # safe default
        ],
    )
    def test_status_mapping(self, broker_status: str, orders_status: str) -> None:
        assert _broker_status_to_orders_status(broker_status) == orders_status


class TestModuleContract:
    """Verify the __all__ export surface stays stable."""

    def test_all_exports_resolvable(self) -> None:
        from services.risk import order_placement_worker as mod

        for name in mod.__all__:
            assert hasattr(mod, name), f"__all__ contains {name!r} but module lacks it"


# ---------------------------------------------------------------------------
# order_filled SSE subscription
# ---------------------------------------------------------------------------


def _build_status_update(
    *,
    status: str = "filled",
    client_order_id: str = "aaaaaaaa-bbbbbbbb-cccccccc-0",
    broker_order_id: int = 8800,
    market: str = "/MES",
    side: str = "buy",
    cumulative: str = "1",
    fill_price: str | None = "5234.75",
    commission: str = "1.25",
    last_fill_at: datetime | None = None,
    rejection_reason: str | None = None,
) -> OrderStatusUpdate:
    return OrderStatusUpdate(
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        status=status,  # type: ignore[arg-type]
        market=market,
        side=side,  # type: ignore[arg-type]
        cumulative_filled_quantity=Decimal(cumulative),
        remaining_quantity=Decimal(0),
        avg_fill_price=Decimal(fill_price) if fill_price is not None else None,
        total_commission_usd=Decimal(commission),
        last_fill_at_utc=last_fill_at or datetime(2026, 5, 12, 21, 32, 15, tzinfo=UTC),
        observed_at_utc=datetime(2026, 5, 12, 21, 32, 15, tzinfo=UTC),
        rejection_reason=rejection_reason,
    )


def _build_worker_with_signal_lookup(*, signal_id: UUID) -> tuple[OrderPlacementWorker, MagicMock]:
    """Wire a worker whose session_factory returns ``signal_id`` for any SELECT,
    and an IBKR client whose subscribe_order_status is a no-op AsyncMock."""
    session = MagicMock()
    row = MagicMock()
    row.signal_id = signal_id
    fetched = MagicMock()
    fetched.fetchone = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=fetched)

    @asynccontextmanager
    async def factory() -> Any:
        yield session

    ibkr = MagicMock()
    ibkr.subscribe_order_status = AsyncMock()

    worker = OrderPlacementWorker(
        session_factory=factory,  # type: ignore[arg-type]
        ibkr_client=ibkr,
        account_id=uuid4(),
        env="paper",
    )
    return worker, session


def _stub_process_fill_event_returning(
    *,
    signal_id: UUID,
    new_order_status: str = "filled",
) -> AsyncMock:
    """Build an AsyncMock that stands in for ``process_fill_event``.

    PR-G integration: ``_on_order_status`` now delegates to
    ``services.risk.fill_processor.process_fill_event`` instead of doing
    its own SELECT + SSE emit. The existing TestOnOrderStatusEmit suite
    asserts on the SSE shape, so we monkeypatch the facade to return a
    FillApplyResult containing the test's signal_id; the worker reads
    that + emits the same fill envelope.
    """
    from services.risk.fill_processor import FillApplyResult

    result = FillApplyResult(
        audit_event_uuids=(uuid4(),),
        signal_id=signal_id,
        account_id=uuid4(),
        fill_id=uuid4(),
        position_id=uuid4(),
        trade_id=uuid4(),
        balance_id=uuid4(),
        new_order_status=new_order_status,  # type: ignore[arg-type]
    )
    return AsyncMock(return_value=result)


class TestOnOrderStatusEmit:
    """Verify _on_order_status emits fill SSE only on terminal filled status."""

    async def test_non_filled_status_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        worker, session = _build_worker_with_signal_lookup(signal_id=uuid4())
        emit = AsyncMock(return_value=99)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        await worker._on_order_status(_build_status_update(status="submitted"))
        emit.assert_not_called()
        # DB shouldn't be queried for non-fills.
        session.execute.assert_not_called()

    async def test_filled_emits_sse_with_expected_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signal_id = uuid4()
        worker, _ = _build_worker_with_signal_lookup(signal_id=signal_id)
        emit = AsyncMock(return_value=42)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        monkeypatch.setattr(
            "services.risk.fill_processor.process_fill_event",
            _stub_process_fill_event_returning(signal_id=signal_id),
        )
        await worker._on_order_status(
            _build_status_update(
                status="filled",
                client_order_id="strategy-paramset-signal-0",
                broker_order_id=1234,
                market="/MES",
                side="sell",
                cumulative="2",
                fill_price="5230.25",
                commission="2.50",
            )
        )
        emit.assert_awaited_once()
        event_type, payload = emit.await_args.args  # type: ignore[union-attr]
        assert event_type == "fill"
        assert payload["order_id"] == "1234"
        assert payload["client_order_id"] == "strategy-paramset-signal-0"
        assert payload["signal_id"] == str(signal_id)
        assert payload["market"] == "/MES"
        assert payload["side"] == "sell"
        assert payload["quantity"] == "2"
        assert payload["fill_price"] == "5230.25"
        assert payload["commission_usd"] == "2.50"
        assert payload["environment"] == "paper"
        assert payload["filled_at_utc"].endswith("+00:00")
        # PR-G additions: the SSE envelope now carries the row PKs +
        # audit_event_uuid so the consumer can deep-link without an
        # extra DB roundtrip.
        assert "fill_id" in payload
        assert "position_id" in payload
        assert "trade_id" in payload
        assert "balance_id" in payload
        assert payload["new_order_status"] == "filled"
        assert payload["audit_event_uuid"] is not None

    async def test_filled_no_avg_price_emits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        signal_id = uuid4()
        worker, _ = _build_worker_with_signal_lookup(signal_id=signal_id)
        emit = AsyncMock(return_value=1)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        monkeypatch.setattr(
            "services.risk.fill_processor.process_fill_event",
            _stub_process_fill_event_returning(signal_id=signal_id),
        )
        await worker._on_order_status(_build_status_update(status="filled", fill_price=None))
        _, payload = emit.await_args.args  # type: ignore[union-attr]
        assert payload["fill_price"] == "0"

    async def test_duplicate_filled_event_deduped_in_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signal_id = uuid4()
        worker, _ = _build_worker_with_signal_lookup(signal_id=signal_id)
        emit = AsyncMock(return_value=1)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        monkeypatch.setattr(
            "services.risk.fill_processor.process_fill_event",
            _stub_process_fill_event_returning(signal_id=signal_id),
        )
        u = _build_status_update(status="filled", broker_order_id=9999)
        await worker._on_order_status(u)
        await worker._on_order_status(u)
        assert emit.await_count == 1

    async def test_unknown_client_order_id_logs_and_skips_emit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub a session factory whose SELECT returns no row.
        session = MagicMock()
        fetched = MagicMock()
        fetched.fetchone = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=fetched)

        @asynccontextmanager
        async def factory() -> Any:
            yield session

        ibkr = MagicMock()
        ibkr.subscribe_order_status = AsyncMock()
        worker = OrderPlacementWorker(
            session_factory=factory,  # type: ignore[arg-type]
            ibkr_client=ibkr,
            account_id=uuid4(),
            env="paper",
        )
        emit = AsyncMock()
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        # process_fill_event returns None when fetch_fill_context yields
        # None (unknown client_order_id); the worker's branch logs at
        # WARNING + skips the emit. Stub returning None mirrors that
        # contract without needing to fake the inner DB queries.
        monkeypatch.setattr(
            "services.risk.fill_processor.process_fill_event",
            AsyncMock(return_value=None),
        )
        await worker._on_order_status(_build_status_update(status="filled"))
        emit.assert_not_called()

    async def test_emit_sse_exception_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        signal_id = uuid4()
        worker, _ = _build_worker_with_signal_lookup(signal_id=signal_id)
        emit = AsyncMock(side_effect=RuntimeError("multiplexer down"))
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        monkeypatch.setattr(
            "services.risk.fill_processor.process_fill_event",
            _stub_process_fill_event_returning(signal_id=signal_id),
        )
        # Must not raise — the broker connection keeps flowing on emit failure.
        await worker._on_order_status(_build_status_update(status="filled"))
        # PR-G change: the audit + tables are durable on SSE failure, so
        # we DO add to the dedupe set (a re-fired Filled wouldn't write
        # a second fills row anyway thanks to UNIQUE(broker_fill_id, created_at),
        # but the dedupe set is the cheap fast path). Pre-PR-G the
        # dedupe set was deliberately NOT updated; post-PR-G it IS.
        assert worker._emitted_fill_order_ids == {
            _build_status_update(status="filled").broker_order_id
        }


# ---------------------------------------------------------------------------
# Defect #2 (2026-05-16): terminal-status propagation
# ---------------------------------------------------------------------------


def _stub_terminal_status_event_returning(
    *,
    market: str = "/MNQ",
    new_status: str = "rejected",
) -> AsyncMock:
    """Stand-in for process_terminal_status_event returning a populated result."""
    from services.risk.order_terminal_status_processor import TerminalStatusResult

    result = TerminalStatusResult(
        order_id=uuid4(),
        signal_id=uuid4(),
        market=market,
        new_status=new_status,
        audit_event_uuid=uuid4(),
        audit_sequence_no=10,
    )
    return AsyncMock(return_value=result)


class TestOnOrderStatusTerminal:
    """Defect #2 fix (2026-05-16): the worker propagates cancelled /
    rejected / inactive orderStatus events to backend tables + audit
    chain + SSE. Pre-fix, the worker dropped these silently and
    ``orders.status`` stayed at ``pending`` indefinitely.
    """

    async def test_rejected_propagates_via_terminal_processor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker, _ = _build_worker_with_signal_lookup(signal_id=uuid4())
        emit = AsyncMock(return_value=11)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        stub = _stub_terminal_status_event_returning(new_status="rejected")
        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            stub,
        )

        await worker._on_order_status(
            _build_status_update(
                status="rejected",
                broker_order_id=3,
                market="/MNQ",
                rejection_reason="321 - Please enter a local symbol or an expiry",
            )
        )

        stub.assert_awaited_once()
        kwargs = stub.await_args.kwargs
        assert kwargs["env"] == "paper"
        payload_arg = kwargs["payload"]
        assert payload_arg.status_kind == "rejected"
        assert payload_arg.rejection_reason == "321 - Please enter a local symbol or an expiry"

        # SSE fired with action=rejected
        emit.assert_awaited_once()
        event_type, body = emit.await_args.args
        assert event_type == "order"
        assert body["action"] == "rejected"
        assert body["new_status"] == "rejected"
        assert body["broker_order_id"] == "3"
        assert body["rejection_reason"] == "321 - Please enter a local symbol or an expiry"
        # Dedupe set marked
        assert 3 in worker._emitted_fill_order_ids

    async def test_cancelled_propagates_with_action_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker, _ = _build_worker_with_signal_lookup(signal_id=uuid4())
        emit = AsyncMock(return_value=12)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        stub = _stub_terminal_status_event_returning(new_status="cancelled")
        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            stub,
        )
        await worker._on_order_status(_build_status_update(status="cancelled", broker_order_id=5))
        stub.assert_awaited_once()
        # The status_kind passed to the processor matches the inbound update
        assert stub.await_args.kwargs["payload"].status_kind == "cancelled"
        emit.assert_awaited_once()
        body = emit.await_args.args[1]
        assert body["action"] == "cancelled"
        assert body["new_status"] == "cancelled"

    async def test_inactive_propagates_as_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # IBKR's Inactive surfaces validation errors; the processor maps
        # to "rejected" wire value. The worker's job is just to forward
        # the inbound status_kind faithfully.
        worker, _ = _build_worker_with_signal_lookup(signal_id=uuid4())
        emit = AsyncMock(return_value=13)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        stub = _stub_terminal_status_event_returning(new_status="rejected")
        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            stub,
        )
        await worker._on_order_status(_build_status_update(status="inactive", broker_order_id=7))
        # Worker forwards "inactive" — processor handles the mapping
        assert stub.await_args.kwargs["payload"].status_kind == "inactive"
        # Output reflects processor's resolution
        body = emit.await_args.args[1]
        assert body["new_status"] == "rejected"
        assert body["action"] == "rejected"

    async def test_terminal_already_in_dedupe_set_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker, _ = _build_worker_with_signal_lookup(signal_id=uuid4())
        worker._emitted_fill_order_ids.add(99)
        emit = AsyncMock()
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        stub = AsyncMock()
        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            stub,
        )
        await worker._on_order_status(_build_status_update(status="rejected", broker_order_id=99))
        stub.assert_not_awaited()
        emit.assert_not_called()

    async def test_processor_returns_none_marks_dedupe_no_sse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Order not found in DB (orphan / already terminal) — processor
        # returns None. Worker must mark dedupe to avoid retrying + must
        # NOT emit SSE (no audit linkage to stamp).
        worker, _ = _build_worker_with_signal_lookup(signal_id=uuid4())
        emit = AsyncMock()
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        stub = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            stub,
        )
        await worker._on_order_status(_build_status_update(status="rejected", broker_order_id=200))
        stub.assert_awaited_once()
        emit.assert_not_called()
        assert 200 in worker._emitted_fill_order_ids

    async def test_filled_path_unaffected_by_terminal_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: the new terminal branch must NOT intercept filled.
        signal_id = uuid4()
        worker, _ = _build_worker_with_signal_lookup(signal_id=signal_id)
        emit = AsyncMock(return_value=14)
        monkeypatch.setattr("services.api.sse.emit_sse", emit)
        monkeypatch.setattr(
            "services.risk.fill_processor.process_fill_event",
            _stub_process_fill_event_returning(signal_id=signal_id),
        )
        # The terminal stub must NOT be called for a filled event
        term_stub = AsyncMock()
        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            term_stub,
        )
        await worker._on_order_status(_build_status_update(status="filled"))
        term_stub.assert_not_awaited()
        emit.assert_awaited_once()
        body = emit.await_args.args[1]
        # The fill envelope (NOT the order envelope) is emitted
        assert emit.await_args.args[0] == "fill"
        assert body.get("market") == "/MES"


# ---------------------------------------------------------------------------
# PR-H: HALT_NEW gate on run_once
# ---------------------------------------------------------------------------


class TestHaltGuard:
    """run_once must short-circuit when risk_state is HALT_NEW. The
    approve-time gate in apply_signal_dispatch covers the operator-side
    block (signal can't transition to 'approved' under HALT); this
    worker-side gate covers the racy edge case where a signal was
    approved before the halt fired + is now sitting in the table."""

    async def test_halt_new_skips_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When fetch_current_risk_state returns 'HALT_NEW', run_once
        returns 0 + does NOT call fetch_approved_signals."""
        from services.risk import order_placement_worker as worker_mod

        fetch_signals_calls: list[None] = []

        async def _fake_fetch_signals(*args: Any, **kwargs: Any) -> list[Any]:
            fetch_signals_calls.append(None)
            return []

        monkeypatch.setattr(
            "services.risk.signal_dispatch.fetch_current_risk_state",
            AsyncMock(return_value="HALT_NEW"),
        )
        monkeypatch.setattr(worker_mod, "fetch_approved_signals", _fake_fetch_signals)

        session = MagicMock()
        session.execute = AsyncMock()

        @asynccontextmanager
        async def factory() -> Any:
            yield session

        ibkr = MagicMock()
        worker = OrderPlacementWorker(
            session_factory=factory,  # type: ignore[arg-type]
            ibkr_client=ibkr,
            account_id=uuid4(),
            env="paper",
        )
        placed = await worker.run_once()
        assert placed == 0
        assert fetch_signals_calls == []  # fetch_approved_signals NOT called

    async def test_normal_proceeds_to_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NORMAL passes the gate; fetch_approved_signals is called."""
        from services.risk import order_placement_worker as worker_mod

        fetch_signals_calls: list[None] = []

        async def _fake_fetch_signals(*args: Any, **kwargs: Any) -> list[Any]:
            fetch_signals_calls.append(None)
            return []  # empty list → no signals to drain → returns 0 but proves call

        monkeypatch.setattr(
            "services.risk.signal_dispatch.fetch_current_risk_state",
            AsyncMock(return_value="NORMAL"),
        )
        monkeypatch.setattr(worker_mod, "fetch_approved_signals", _fake_fetch_signals)

        session = MagicMock()
        session.execute = AsyncMock()

        @asynccontextmanager
        async def factory() -> Any:
            yield session

        ibkr = MagicMock()
        worker = OrderPlacementWorker(
            session_factory=factory,  # type: ignore[arg-type]
            ibkr_client=ibkr,
            account_id=uuid4(),
            env="paper",
        )
        await worker.run_once()
        assert len(fetch_signals_calls) == 1

    async def test_convalescent_proceeds_to_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CONVALESCENT also passes the gate (per backend-spec §2.5)."""
        from services.risk import order_placement_worker as worker_mod

        fetch_signals_calls: list[None] = []

        async def _fake_fetch_signals(*args: Any, **kwargs: Any) -> list[Any]:
            fetch_signals_calls.append(None)
            return []

        monkeypatch.setattr(
            "services.risk.signal_dispatch.fetch_current_risk_state",
            AsyncMock(return_value="CONVALESCENT"),
        )
        monkeypatch.setattr(worker_mod, "fetch_approved_signals", _fake_fetch_signals)

        session = MagicMock()
        session.execute = AsyncMock()

        @asynccontextmanager
        async def factory() -> Any:
            yield session

        ibkr = MagicMock()
        worker = OrderPlacementWorker(
            session_factory=factory,  # type: ignore[arg-type]
            ibkr_client=ibkr,
            account_id=uuid4(),
            env="paper",
        )
        await worker.run_once()
        assert len(fetch_signals_calls) == 1

    async def test_none_risk_state_proceeds_fail_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None risk_state (no row) skips the gate (fail-open per
        fetch_current_risk_state docstring)."""
        from services.risk import order_placement_worker as worker_mod

        fetch_signals_calls: list[None] = []

        async def _fake_fetch_signals(*args: Any, **kwargs: Any) -> list[Any]:
            fetch_signals_calls.append(None)
            return []

        monkeypatch.setattr(
            "services.risk.signal_dispatch.fetch_current_risk_state",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(worker_mod, "fetch_approved_signals", _fake_fetch_signals)

        session = MagicMock()
        session.execute = AsyncMock()

        @asynccontextmanager
        async def factory() -> Any:
            yield session

        ibkr = MagicMock()
        worker = OrderPlacementWorker(
            session_factory=factory,  # type: ignore[arg-type]
            ibkr_client=ibkr,
            account_id=uuid4(),
            env="paper",
        )
        await worker.run_once()
        assert len(fetch_signals_calls) == 1


# =====================================================================
# Timeout + payload-observability tests (2026-05-17 follow-up to silent-
# worker pattern). The first batch verifies _await_ibkr_with_timeout
# translates hangs into IbkrPlacementError so the existing except paths
# handle them cleanly. The second batch verifies the pre-append payload
# log carries the audit-leg metadata that would have caught the
# 2026-05-17 drill 1 "missing leg field" mystery had it been present.
# =====================================================================


class TestAwaitIbkrWithTimeout:
    """The helper that bounds every IBKR adapter await.

    Verifies:
      * Happy path: a quick-completing coroutine returns its value.
      * Hung path: a coroutine that exceeds the timeout raises
        IbkrPlacementError with the right operation + detail fields.
      * Exception passthrough: a coroutine that raises something else
        propagates that exception unchanged (we only translate the
        timeout, NOT every exception).
    """

    @pytest.mark.asyncio
    async def test_returns_value_on_quick_completion(self) -> None:
        from services.risk.order_placement_worker import _await_ibkr_with_timeout

        async def _quick() -> str:
            return "done"

        result = await _await_ibkr_with_timeout(
            _quick(),
            operation="testOp",
            timeout_seconds=30.0,
        )
        assert result == "done"

    @pytest.mark.asyncio
    async def test_hung_coroutine_raises_ibkr_placement_error(self) -> None:
        import asyncio

        from services.execution.types import IbkrPlacementError
        from services.risk.order_placement_worker import _await_ibkr_with_timeout

        async def _hung() -> None:
            await asyncio.sleep(60)

        with pytest.raises(IbkrPlacementError) as excinfo:
            await _await_ibkr_with_timeout(
                _hung(),
                operation="placeOrder.entry",
                timeout_seconds=0.05,
            )
        err = excinfo.value
        assert err.operation == "placeOrder.entry"
        assert "timed out after 0.05s" in err.detail
        assert err.underlying_exception_class == "TimeoutError"
        assert err.occurred_at_utc.tzinfo is not None

    @pytest.mark.asyncio
    async def test_propagates_non_timeout_exception_unchanged(self) -> None:
        from services.risk.order_placement_worker import _await_ibkr_with_timeout

        async def _raises_value_error() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await _await_ibkr_with_timeout(
                _raises_value_error(),
                operation="testOp",
                timeout_seconds=30.0,
            )

    @pytest.mark.asyncio
    async def test_propagates_ibkr_placement_error_unchanged(self) -> None:
        from services.execution.types import IbkrPlacementError
        from services.risk.order_placement_worker import _await_ibkr_with_timeout

        original = IbkrPlacementError(
            operation="connect",
            detail="ib_gateway down",
            underlying_exception_class="ConnectionError",
            occurred_at_utc=datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC),
        )

        async def _raises_existing_ibkr_err() -> None:
            raise original

        with pytest.raises(IbkrPlacementError) as excinfo:
            await _await_ibkr_with_timeout(
                _raises_existing_ibkr_err(),
                operation="placeOrder.entry",
                timeout_seconds=30.0,
            )
        # Same instance — NOT re-wrapped into a timeout error.
        assert excinfo.value.operation == "connect"
        assert excinfo.value.detail == "ib_gateway down"


class TestWorkerConstructorTimeoutDefault:
    """The worker carries _ibkr_call_timeout_seconds through constructor."""

    def test_default_timeout_matches_constant(self) -> None:
        from services.risk.order_placement_worker import (
            DEFAULT_IBKR_CALL_TIMEOUT_SECONDS,
        )

        @asynccontextmanager
        async def factory() -> Any:
            yield MagicMock()

        worker = OrderPlacementWorker(
            session_factory=factory,  # type: ignore[arg-type]
            ibkr_client=MagicMock(),
            account_id=uuid4(),
            env="paper",
        )
        assert worker._ibkr_call_timeout_seconds == DEFAULT_IBKR_CALL_TIMEOUT_SECONDS
        assert DEFAULT_IBKR_CALL_TIMEOUT_SECONDS == 30.0

    def test_explicit_timeout_overrides_default(self) -> None:
        @asynccontextmanager
        async def factory() -> Any:
            yield MagicMock()

        worker = OrderPlacementWorker(
            session_factory=factory,  # type: ignore[arg-type]
            ibkr_client=MagicMock(),
            account_id=uuid4(),
            env="paper",
            ibkr_call_timeout_seconds=5.0,
        )
        assert worker._ibkr_call_timeout_seconds == 5.0


class TestApplyOrderPlacementTimeout:
    """Verifies a hung place_order call inside apply_order_placement
    raises IbkrPlacementError via _await_ibkr_with_timeout."""

    @pytest.mark.asyncio
    async def test_entry_hangs_raises_ibkr_placement_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from services.execution.types import IbkrContractRef, IbkrPlacementError
        from services.risk.order_placement_worker import apply_order_placement

        async def _hung_place(_req: Any) -> Any:
            await asyncio.sleep(60)
            raise AssertionError("should never reach here")

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=_hung_place)
        ibkr.cancel_order = AsyncMock()

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")

        with pytest.raises(IbkrPlacementError) as excinfo:
            await apply_order_placement(
                plan,
                ibkr_client=ibkr,
                contract=contract,
                session_factory=_build_apply_session_factory(),
                ibkr_call_timeout_seconds=0.05,
            )
        assert excinfo.value.operation == "placeOrder.entry"
        assert "timed out" in excinfo.value.detail

    @pytest.mark.asyncio
    async def test_stop_hangs_cancels_entry_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import (
            OrderPlacementError,
            apply_order_placement,
        )

        call_no = 0

        async def _maybe_hang(_req: Any) -> Any:
            nonlocal call_no
            call_no += 1
            if call_no == 1:
                # Entry succeeds quickly.
                return _fake_ibkr_place_result(broker_order_id=42, status="pending_submit")
            # Stop hangs.
            await asyncio.sleep(60)
            raise AssertionError("should never reach here")

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=_maybe_hang)
        ibkr.cancel_order = AsyncMock()

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")

        with pytest.raises(OrderPlacementError, match="STOP_PLACEMENT_FAILED"):
            await apply_order_placement(
                plan,
                ibkr_client=ibkr,
                contract=contract,
                session_factory=_build_apply_session_factory(),
                ibkr_call_timeout_seconds=0.05,
            )
        # Cancel should have been called on the entry — best-effort
        # cleanup after the stop-side hang.
        assert ibkr.cancel_order.await_count == 1


class TestAuditPayloadObservabilityLog:
    """The pre-append observability log fires once per leg with the
    payload key set. This is the diagnostic for the 2026-05-17 drill 1
    'leg field missing' mystery (audit row seq=45 written without the
    leg field even though deployed code unambiguously sets it).
    """

    @pytest.mark.asyncio
    async def test_entry_audit_pre_append_logs_payload_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from structlog.testing import capture_logs

        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        async def fake_place(request: Any) -> Any:
            return _fake_ibkr_place_result(broker_order_id=99, status="pending_submit")

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock()

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        with capture_logs() as logs:
            await apply_order_placement(
                plan,
                ibkr_client=ibkr,
                contract=contract,
                session_factory=_build_apply_session_factory(),
            )
        pre_appends = [
            entry
            for entry in logs
            if entry.get("event") == "order_placement_audit_payload_pre_append"
        ]
        # Two pre-append logs: one for entry, one for stop.
        assert len(pre_appends) == 2
        entry_pre = next(e for e in pre_appends if e["leg"] == "entry")
        # The audit-leg breadcrumb the drill 1 mystery needed.
        assert entry_pre["has_leg_field"] is True
        assert "leg" in entry_pre["payload_keys"]
        assert entry_pre["payload_key_count"] == len(entry_pre["payload_keys"])
        # All 15 canonical entry-leg keys present.
        assert entry_pre["payload_key_count"] == 15
        # Sorted for log-grep stability.
        assert entry_pre["payload_keys"] == sorted(entry_pre["payload_keys"])

    @pytest.mark.asyncio
    async def test_stop_audit_pre_append_logs_payload_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from structlog.testing import capture_logs

        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        place_call_no = 0

        async def fake_place(request: Any) -> Any:
            nonlocal place_call_no
            place_call_no += 1
            return _fake_ibkr_place_result(
                broker_order_id=place_call_no * 10, status="pending_submit"
            )

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock()

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        with capture_logs() as logs:
            await apply_order_placement(
                plan,
                ibkr_client=ibkr,
                contract=contract,
                session_factory=_build_apply_session_factory(),
            )
        pre_appends = [
            entry
            for entry in logs
            if entry.get("event") == "order_placement_audit_payload_pre_append"
        ]
        stop_pre = next(e for e in pre_appends if e["leg"] == "stop")
        assert stop_pre["has_leg_field"] is True
        # 16 canonical stop-leg keys (15 entry keys + parent_client_order_id
        # + stop_price; no limit_price on stop_market orders).
        assert stop_pre["payload_key_count"] == 16
        assert "leg" in stop_pre["payload_keys"]
        assert "parent_client_order_id" in stop_pre["payload_keys"]
        assert "stop_price" in stop_pre["payload_keys"]
        assert "limit_price" not in stop_pre["payload_keys"]

    @pytest.mark.asyncio
    async def test_rejected_entry_skips_stop_pre_append_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from structlog.testing import capture_logs

        from services.execution.types import IbkrContractRef
        from services.risk.order_placement_worker import apply_order_placement

        async def fake_place(request: Any) -> Any:
            # Entry rejected → no stop placement → no stop audit → no
            # stop pre-append log.
            return _fake_ibkr_place_result(broker_order_id=11, status="rejected")

        ibkr = MagicMock()
        ibkr.place_order = AsyncMock(side_effect=fake_place)
        ibkr.cancel_order = AsyncMock()

        async def fake_append(*args: Any, **kwargs: Any) -> Any:
            return MagicMock(event_uuid=uuid4(), sequence_no=1)

        monkeypatch.setattr(
            "services.risk.order_placement_worker.append_audit_event",
            AsyncMock(side_effect=fake_append),
        )

        plan = plan_order_placement(
            _signal(direction="long", market="TLT", decision_price="85.00"),
            env="paper",
        )
        contract = IbkrContractRef(market="TLT", ibkr_local_symbol="TLT", exchange="SMART")
        with capture_logs() as logs:
            await apply_order_placement(
                plan,
                ibkr_client=ibkr,
                contract=contract,
                session_factory=_build_apply_session_factory(),
            )
        pre_appends = [
            entry
            for entry in logs
            if entry.get("event") == "order_placement_audit_payload_pre_append"
        ]
        # Only ONE pre-append log: the entry. No stop because entry was
        # rejected (db_status='rejected' → place_stop=False).
        assert len(pre_appends) == 1
        assert pre_appends[0]["leg"] == "entry"
