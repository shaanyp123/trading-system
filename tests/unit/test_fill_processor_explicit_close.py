"""Exit-pipeline PR-C (2026-05-27) unit tests for fill_processor's
classifier widening.

The fill classifier (:func:`_classify_fill_scenario`) is now widened
to handle explicit-exit fills where ``signal_direction='flat'`` (the
strategy emits FLAT for explicit exits per design §5.2; PR-B persists
'flat' on the signals row + PR-C reads it through to FillContext).

Tests prove:

  * Long position + signal_direction='flat' + opposite (sell) fill
    classifies as EXIT_FULL_CLOSE — same as the bracket-stop path.
  * Short position + signal_direction='flat' + opposite (buy) fill
    classifies as EXIT_FULL_CLOSE.
  * Defensive: signal_direction='flat' with zero-prior OR same-side
    prior raises UnsupportedFillScenarioError (orphan exit fill = an
    upstream invariant break).
  * Pre-PR-C bracket-stop fills (signal_direction='long'/'short' on
    the exit row via Option B contract) STILL classify as
    EXIT_FULL_CLOSE — no regression.
  * Entry path mismatches still raise (no regression).

A22 enforced (no I/O — pure policy only).
"""

from __future__ import annotations

import pytest

from services.risk.fill_processor import (
    UnsupportedFillScenarioError,
    _classify_fill_scenario,
)


class TestClassifyExitFlatSignal:
    """Exit signals with signal_direction='flat' (PR-C explicit-exit
    contract) classify as EXIT_FULL_CLOSE."""

    def test_long_position_flat_sell_fill_is_exit_full_close(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="flat",
            order_direction="sell",
            prior_position_quantity=5,
            fill_quantity=5,
        )
        assert scenario == "EXIT_FULL_CLOSE"

    def test_short_position_flat_buy_fill_is_exit_full_close(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="flat",
            order_direction="buy",
            prior_position_quantity=-7,
            fill_quantity=7,
        )
        assert scenario == "EXIT_FULL_CLOSE"


class TestClassifyExitFlatSignalDefensive:
    """Orphan exit fills (signal_direction='flat' on the entry branch
    of the classifier) raise — they imply an upstream invariant break."""

    def test_flat_with_zero_prior_raises(self) -> None:
        """No prior position + flat-direction signal = orphan fill.
        The order_placement_worker's POSITION_ALREADY_FLAT guard
        should have caught this; if we reach here it's a bug."""
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            _classify_fill_scenario(
                signal_direction="flat",
                order_direction="sell",
                prior_position_quantity=0,
                fill_quantity=3,
            )
        assert "flat" in str(exc_info.value).lower()
        assert exc_info.value.details["signal_direction"] == "flat"
        assert exc_info.value.details["prior_position_quantity"] == 0

    def test_flat_with_same_sided_prior_raises(self) -> None:
        """Long prior + sell fill is opposite (correct exit). But a
        long prior + BUY fill is same-direction — a signal_direction
        of 'flat' here is meaningless. Raise."""
        with pytest.raises(UnsupportedFillScenarioError):
            _classify_fill_scenario(
                signal_direction="flat",
                order_direction="buy",  # same-sided as the long prior
                prior_position_quantity=5,
                fill_quantity=3,
            )


class TestClassifyEntryUnchanged:
    """Entry-path classifications still work as pre-PR-C."""

    def test_long_entry_first_fill_is_entry(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="long",
            order_direction="buy",
            prior_position_quantity=0,
            fill_quantity=3,
        )
        assert scenario == "ENTRY"

    def test_short_entry_first_fill_is_entry(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="short",
            order_direction="sell",
            prior_position_quantity=0,
            fill_quantity=3,
        )
        assert scenario == "ENTRY"

    def test_long_add_to_long_is_entry(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="long",
            order_direction="buy",
            prior_position_quantity=2,
            fill_quantity=3,
        )
        assert scenario == "ENTRY"

    def test_short_add_to_short_is_entry(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="short",
            order_direction="sell",
            prior_position_quantity=-2,
            fill_quantity=3,
        )
        assert scenario == "ENTRY"

    def test_long_signal_sell_order_raises(self) -> None:
        """Direction mismatch — entry-side defense."""
        with pytest.raises(UnsupportedFillScenarioError):
            _classify_fill_scenario(
                signal_direction="long",
                order_direction="sell",
                prior_position_quantity=0,
                fill_quantity=3,
            )


class TestClassifyBracketStopFillUnchanged:
    """Pre-PR-C bracket-stop fills classify as EXIT_FULL_CLOSE — no
    regression. The bracket-stop reuses the entry signal_id per Option
    B contract, so signal_direction is the entry's long/short."""

    def test_bracket_stop_long_exit_is_exit_full_close(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="long",
            order_direction="sell",
            prior_position_quantity=5,
            fill_quantity=5,
        )
        assert scenario == "EXIT_FULL_CLOSE"

    def test_bracket_stop_short_exit_is_exit_full_close(self) -> None:
        scenario = _classify_fill_scenario(
            signal_direction="short",
            order_direction="buy",
            prior_position_quantity=-3,
            fill_quantity=3,
        )
        assert scenario == "EXIT_FULL_CLOSE"


class TestClassifyDeferredExitPaths:
    """Partial exits + reversals are deferred to Phase 2+ — still raise."""

    def test_partial_exit_raises(self) -> None:
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            _classify_fill_scenario(
                signal_direction="flat",
                order_direction="sell",
                prior_position_quantity=5,
                fill_quantity=3,  # partial
            )
        assert exc_info.value.error_code == "FILL_EXIT_CASE_DEFERRED"
        assert "Partial exit" in str(exc_info.value) or "partial" in str(exc_info.value).lower()

    def test_reversal_raises(self) -> None:
        with pytest.raises(UnsupportedFillScenarioError) as exc_info:
            _classify_fill_scenario(
                signal_direction="flat",
                order_direction="sell",
                prior_position_quantity=5,
                fill_quantity=8,  # reversal — would cross zero
            )
        assert exc_info.value.error_code == "FILL_EXIT_CASE_DEFERRED"


class TestFillContextSignalTypeField:
    """FillContext now carries signal_type — defaults to 'entry'."""

    def test_signal_type_default_entry(self) -> None:
        from datetime import UTC, datetime
        from decimal import Decimal
        from uuid import uuid4

        from services.risk.fill_processor import FillContext

        ctx = FillContext(
            order_id=uuid4(),
            order_created_at=datetime.now(tz=UTC),
            signal_id=uuid4(),
            account_id=uuid4(),
            env="paper",
            market="/MES",
            contract_id=None,
            signal_direction="long",
            order_direction="buy",
            target_contracts=3,
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            slippage_calibration_version_id=uuid4(),
            decision_price=Decimal("4500.00"),
            managed_by_version="a" * 40,
        )
        assert ctx.signal_type == "entry"

    def test_signal_type_exit_explicit(self) -> None:
        from datetime import UTC, datetime
        from decimal import Decimal
        from uuid import uuid4

        from services.risk.fill_processor import FillContext

        ctx = FillContext(
            order_id=uuid4(),
            order_created_at=datetime.now(tz=UTC),
            signal_id=uuid4(),
            account_id=uuid4(),
            env="paper",
            market="/MES",
            contract_id=None,
            signal_direction="flat",
            order_direction="sell",
            target_contracts=0,
            strategy_hash="a" * 40,
            parameter_set_hash="b" * 64,
            slippage_calibration_version_id=uuid4(),
            decision_price=Decimal("4500.00"),
            managed_by_version="a" * 40,
            signal_type="exit",
        )
        assert ctx.signal_type == "exit"
        assert ctx.signal_direction == "flat"
