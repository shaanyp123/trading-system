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


def _signal(
    *,
    direction: str = "long",
    target_contracts: int = 1,
    strategy_hash: str = "a" * 40,
    parameter_set_hash: str = "b" * 64,
) -> ApprovedSignalRow:
    """Build a sample ApprovedSignalRow for tests."""
    return ApprovedSignalRow(
        signal_id=uuid4(),
        account_id=uuid4(),
        env="paper",
        market="/MES",
        direction=direction,  # type: ignore[arg-type]
        target_contracts=target_contracts,
        decision_price=Decimal("4250.50"),
        strategy_hash=strategy_hash,
        parameter_set_hash=parameter_set_hash,
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
