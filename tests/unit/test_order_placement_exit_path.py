"""Exit-pipeline PR-C (2026-05-27) unit tests for the order_placement
worker's exit-close planner + close-side dataclasses.

Covers the PURE-POLICY surface (:func:`plan_exit_close_placement` +
:class:`ExitClosePlan` shape). The I/O orchestrator
(:func:`apply_exit_close_placement`) is exercised against a real DB
in ``tests/integration/test_exit_end_to_end.py`` (deferred — see PR
description).

Tests prove:

  * Long-position exit: close_side='sell', close_quantity=|prior|.
  * Short-position exit: close_side='buy', close_quantity=|prior|.
  * Zero prior position raises POSITION_ALREADY_FLAT with error_code.
  * Wrong signal_type raises WRONG_SIGNAL_TYPE.
  * The plan's parent_entry_order_id chains to the bracket-stop's
    parent (NOT the bracket-stop itself) so the audit graph reads
    entry → bracket_stop (cancelled) + entry → close (in flight).
  * Tick-rounding of the marketable limit price.
  * client_order_id is deterministic + carries the '-close' suffix.

A22 enforced (no I/O — pure policy only). A05 enforced (Decimal-only
money throughout the planner inputs + outputs).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from services.risk.order_placement_worker import (
    ApprovedSignalRow,
    BracketStopRow,
    OrderPlacementError,
    plan_exit_close_placement,
)

# Test fixtures
_SIGNAL_ID = uuid4()
_ACCOUNT_ID = uuid4()
_ENTRY_ORDER_ID = uuid4()
_BRACKET_STOP_ORDER_ID = uuid4()
_BRACKET_STOP_CREATED_AT = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _exit_signal(
    *,
    market: str = "/MES",
    decision_price: Decimal = Decimal("4500.00"),
) -> ApprovedSignalRow:
    return ApprovedSignalRow(
        signal_id=_SIGNAL_ID,
        account_id=_ACCOUNT_ID,
        env="paper",
        market=market,
        direction="flat",  # exits are direction=FLAT per design §5.2
        target_contracts=0,  # dispatcher-side sizing per §Q3
        decision_price=decision_price,
        strategy_hash="a" * 40,
        parameter_set_hash="b" * 64,
        sizing_trace={},  # exits don't carry stage_0 sizing
        signal_type="exit",
    )


def _bracket_stop(
    *,
    stop_price: Decimal = Decimal("4450.00"),
    broker_order_id: str | None = "BO-123",
) -> BracketStopRow:
    return BracketStopRow(
        order_id=_BRACKET_STOP_ORDER_ID,
        order_created_at=_BRACKET_STOP_CREATED_AT,
        client_order_id="aaaaaaaa-bbbbbbbb-12345678-0-stop",
        broker_order_id=broker_order_id,
        stop_price=stop_price,
        parent_order_id=_ENTRY_ORDER_ID,
    )


class TestPlanExitCloseLongPosition:
    """Long position (+N) → exit close as sell-N at decision_price."""

    def test_close_side_is_sell(self) -> None:
        plan = plan_exit_close_placement(
            _exit_signal(),
            bracket_stop=_bracket_stop(),
            current_position_quantity=5,
            env="paper",
        )
        assert plan.close_side == "sell"
        assert plan.close_quantity == Decimal(5)
        assert plan.prior_position_direction == "long"
        assert plan.prior_position_quantity == 5

    def test_close_limit_price_from_signal_decision_price(self) -> None:
        plan = plan_exit_close_placement(
            _exit_signal(market="/MES", decision_price=Decimal("4500.50")),
            bracket_stop=_bracket_stop(),
            current_position_quantity=3,
            env="paper",
        )
        # /MES tick = 0.25. 4500.50 rounds to 4500.50 (already on-tick).
        assert plan.close_limit_price == Decimal("4500.50")

    def test_close_limit_price_tick_rounded(self) -> None:
        plan = plan_exit_close_placement(
            _exit_signal(market="/MES", decision_price=Decimal("4500.30")),
            bracket_stop=_bracket_stop(),
            current_position_quantity=3,
            env="paper",
        )
        # /MES tick = 0.25. 4500.30 should round to nearest tick.
        # round_to_tick may go up or down depending on rounding policy;
        # we just assert the result is on the tick grid.
        assert (plan.close_limit_price % Decimal("0.25")) == Decimal(0)

    def test_parent_entry_order_id_chain(self) -> None:
        """The close orders row's parent_order_id chains to the
        bracket-stop's parent_order_id (= original entry order id),
        NOT to the bracket-stop's own id."""
        plan = plan_exit_close_placement(
            _exit_signal(),
            bracket_stop=_bracket_stop(),
            current_position_quantity=5,
            env="paper",
        )
        assert plan.parent_entry_order_id == _ENTRY_ORDER_ID
        # Bracket-stop linkage is preserved separately for the cancel
        # step, but the parent edge is the entry order.
        assert plan.bracket_stop_order_id == _BRACKET_STOP_ORDER_ID

    def test_client_order_id_close_suffix(self) -> None:
        plan = plan_exit_close_placement(
            _exit_signal(),
            bracket_stop=_bracket_stop(),
            current_position_quantity=5,
            env="paper",
        )
        assert plan.close_client_order_id.endswith("-close")
        # Distinguishable from the bracket-stop's '-stop' suffix
        assert plan.close_client_order_id != _bracket_stop().client_order_id


class TestPlanExitCloseShortPosition:
    """Short position (-N) → exit close as buy-N."""

    def test_close_side_is_buy(self) -> None:
        plan = plan_exit_close_placement(
            _exit_signal(),
            bracket_stop=_bracket_stop(stop_price=Decimal("4600.00")),
            current_position_quantity=-7,
            env="paper",
        )
        assert plan.close_side == "buy"
        assert plan.close_quantity == Decimal(7)
        assert plan.prior_position_direction == "short"
        assert plan.prior_position_quantity == -7


class TestPlanExitClosePositionAlreadyFlat:
    """Zero position raises with error_code='POSITION_ALREADY_FLAT'."""

    def test_zero_quantity_raises(self) -> None:
        with pytest.raises(OrderPlacementError) as exc_info:
            plan_exit_close_placement(
                _exit_signal(),
                bracket_stop=_bracket_stop(),
                current_position_quantity=0,
                env="paper",
            )
        assert exc_info.value.error_code == "POSITION_ALREADY_FLAT"
        # Detail payload carries the signal_id + market for log triage.
        assert exc_info.value.details["signal_id"] == str(_SIGNAL_ID)
        assert exc_info.value.details["market"] == "/MES"


class TestPlanExitCloseWrongSignalType:
    """Defensive: planner called with signal_type='entry' raises."""

    def test_entry_signal_type_raises(self) -> None:
        entry_signal = ApprovedSignalRow(
            signal_id=_SIGNAL_ID,
            account_id=_ACCOUNT_ID,
            env="paper",
            market="/MES",
            direction="long",
            target_contracts=3,
            decision_price=Decimal("4500.00"),
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            sizing_trace={},
            signal_type="entry",
        )
        with pytest.raises(OrderPlacementError) as exc_info:
            plan_exit_close_placement(
                entry_signal,
                bracket_stop=_bracket_stop(),
                current_position_quantity=3,
                env="paper",
            )
        assert exc_info.value.error_code == "WRONG_SIGNAL_TYPE"


class TestPlanExitCloseUnknownMarket:
    """Tick-rounding fails for unknown markets — clear error."""

    def test_unknown_market_raises(self) -> None:
        with pytest.raises(OrderPlacementError) as exc_info:
            plan_exit_close_placement(
                _exit_signal(market="/UNKNOWN_MARKET"),
                bracket_stop=_bracket_stop(),
                current_position_quantity=5,
                env="paper",
            )
        assert exc_info.value.error_code == "UNKNOWN_MARKET_TICK"


class TestBracketStopRowShape:
    """Light contract checks on the BracketStopRow dataclass."""

    def test_frozen_and_slotted(self) -> None:
        row = _bracket_stop()
        # Frozen → can't mutate
        with pytest.raises(Exception):
            row.stop_price = Decimal("9999.99")  # type: ignore[misc]
        # Slotted → no __dict__
        assert not hasattr(row, "__dict__")


class TestModuleContract:
    """Public surface — the worker module exports the exit-path types
    + planner + helpers."""

    def test_exit_path_public_surface(self) -> None:
        from services.risk import order_placement_worker

        for name in (
            "ApprovedSignalRow",
            "BracketStopRow",
            "ExitClosePlan",
            "OrderPlacementError",
            "PositionUnprotectedAlertDescriptor",
            "PositionUnprotectedAlertHook",
            "apply_exit_close_placement",
            "fetch_current_position_quantity",
            "fetch_entry_signal_id_for_open_trade",
            "fetch_open_bracket_stop_for_entry",
            "plan_exit_close_placement",
        ):
            assert hasattr(order_placement_worker, name), f"missing public {name}"
