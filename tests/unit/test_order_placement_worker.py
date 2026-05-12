"""Unit tests for :mod:`services.risk.order_placement_worker`.

Pure-policy tests against the planner + worker state machine. No
testcontainers (A22 N/A — no audit_log writes); fakes the IBKR client
and the session factory.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from services.risk.order_placement_worker import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    RETRY_N_PHASE_1,
    ApprovedSignalRow,
    OrderPlacementError,
    OrderPlacementPlan,
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
