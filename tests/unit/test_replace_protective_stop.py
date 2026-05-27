"""Unit tests for scripts.operator_tools.replace_protective_stop.

Covers the pure-policy surface (``plan_replace_protective_stop`` +
``_build_replace_stop_client_order_id`` +
``_extract_stop_price_from_sizing_trace`` + ``_parse_args``). The I/O
orchestrator (``apply_replace_protective_stop``) is exercised against
a real Postgres + mocked IbkrClient in an integration test that is
DEFERRED per the PR description (testcontainers Postgres + mocked
broker is similar surface to replay_executions's deferred path).

A22 enforced (no DB / IBKR calls — pure policy + argparse only).
A05 enforced (Decimal-only money throughout).
A06 enforced (tz-aware UTC for the few datetime references).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from scripts.operator_tools.replace_protective_stop import (
    EXIT_BAD_ARGS,
    OpenPositionSnapshot,
    OpenTradeSnapshot,
    ReplaceProtectiveStopError,
    _broker_status_to_orders_status,
    _build_replace_stop_client_order_id,
    _extract_stop_price_from_sizing_trace,
    _parse_args,
    main,
    plan_replace_protective_stop,
)

_ACCOUNT_ID = uuid4()
_ENTRY_SIGNAL_ID = uuid4()
_ENTRY_ORDER_ID = uuid4()
_POSITION_ID = uuid4()
_TRADE_ID = uuid4()
_SLIPPAGE_VERSION_ID = uuid4()

_STRATEGY_HASH = "a" * 40
_PARAMETER_HASH = "b" * 64


def _long_position(*, qty: int = 5, avg_cost: Decimal = Decimal("2880.00")) -> OpenPositionSnapshot:
    return OpenPositionSnapshot(
        position_id=_POSITION_ID,
        account_id=_ACCOUNT_ID,
        market="/MES",
        contract_id=None,
        quantity=qty,
        avg_cost=avg_cost,
    )


def _short_position(
    *, qty: int = -3, avg_cost: Decimal = Decimal("100.00")
) -> OpenPositionSnapshot:
    return OpenPositionSnapshot(
        position_id=_POSITION_ID,
        account_id=_ACCOUNT_ID,
        market="/MES",
        contract_id=None,
        quantity=qty,
        avg_cost=avg_cost,
    )


def _trade() -> OpenTradeSnapshot:
    return OpenTradeSnapshot(
        trade_id=_TRADE_ID,
        entry_signal_id=_ENTRY_SIGNAL_ID,
        entry_order_id=_ENTRY_ORDER_ID,
        strategy_hash=_STRATEGY_HASH,
        parameter_set_hash=_PARAMETER_HASH,
        slippage_calibration_version_id=_SLIPPAGE_VERSION_ID,
    )


def _sizing_trace_with_stop(stop_price: str, *, market: str = "/MES") -> dict[str, object]:
    return {
        "stage_0_universe": {
            "strategy_inputs": {
                market: {
                    "stop_price": stop_price,
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# _build_replace_stop_client_order_id
# ---------------------------------------------------------------------------


class TestBuildClientOrderId:
    def test_format(self) -> None:
        cid = _build_replace_stop_client_order_id(
            strategy_hash=_STRATEGY_HASH,
            parameter_set_hash=_PARAMETER_HASH,
            entry_signal_id=_ENTRY_SIGNAL_ID,
            retry_n=0,
        )
        assert cid.endswith("-stop-replace-0")
        assert cid.startswith("aaaaaaaa-bbbbbbbb-")
        # Distinguishable from the original entry CID + bracket-stop CID
        assert "-stop-replace-" in cid

    def test_deterministic_per_signal(self) -> None:
        cid1 = _build_replace_stop_client_order_id(
            strategy_hash=_STRATEGY_HASH,
            parameter_set_hash=_PARAMETER_HASH,
            entry_signal_id=_ENTRY_SIGNAL_ID,
            retry_n=0,
        )
        cid2 = _build_replace_stop_client_order_id(
            strategy_hash=_STRATEGY_HASH,
            parameter_set_hash=_PARAMETER_HASH,
            entry_signal_id=_ENTRY_SIGNAL_ID,
            retry_n=0,
        )
        assert cid1 == cid2

    def test_retry_n_differentiates(self) -> None:
        cid0 = _build_replace_stop_client_order_id(
            strategy_hash=_STRATEGY_HASH,
            parameter_set_hash=_PARAMETER_HASH,
            entry_signal_id=_ENTRY_SIGNAL_ID,
            retry_n=0,
        )
        cid1 = _build_replace_stop_client_order_id(
            strategy_hash=_STRATEGY_HASH,
            parameter_set_hash=_PARAMETER_HASH,
            entry_signal_id=_ENTRY_SIGNAL_ID,
            retry_n=1,
        )
        assert cid0 != cid1
        assert cid0.endswith("-stop-replace-0")
        assert cid1.endswith("-stop-replace-1")

    def test_short_strategy_hash_raises(self) -> None:
        with pytest.raises(ValueError, match="strategy_hash"):
            _build_replace_stop_client_order_id(
                strategy_hash="abc",
                parameter_set_hash=_PARAMETER_HASH,
                entry_signal_id=_ENTRY_SIGNAL_ID,
                retry_n=0,
            )

    def test_retry_n_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="retry_n"):
            _build_replace_stop_client_order_id(
                strategy_hash=_STRATEGY_HASH,
                parameter_set_hash=_PARAMETER_HASH,
                entry_signal_id=_ENTRY_SIGNAL_ID,
                retry_n=10,
            )


# ---------------------------------------------------------------------------
# _extract_stop_price_from_sizing_trace
# ---------------------------------------------------------------------------


class TestExtractStopPriceFromSizingTrace:
    def test_canonical_path(self) -> None:
        trace = _sizing_trace_with_stop("2770.20")
        result = _extract_stop_price_from_sizing_trace(trace, market="/MES")
        assert result == Decimal("2770.20")

    def test_missing_path_returns_none(self) -> None:
        assert _extract_stop_price_from_sizing_trace({}, market="/MES") is None
        assert (
            _extract_stop_price_from_sizing_trace({"stage_0_universe": {}}, market="/MES") is None
        )
        assert (
            _extract_stop_price_from_sizing_trace(
                {"stage_0_universe": {"strategy_inputs": {}}}, market="/MES"
            )
            is None
        )

    def test_wrong_market_returns_none(self) -> None:
        trace = _sizing_trace_with_stop("2770.20", market="/MES")
        assert _extract_stop_price_from_sizing_trace(trace, market="/MNQ") is None

    def test_empty_value_returns_none(self) -> None:
        trace = _sizing_trace_with_stop("", market="/MES")
        assert _extract_stop_price_from_sizing_trace(trace, market="/MES") is None

    def test_unparseable_returns_none(self) -> None:
        trace = _sizing_trace_with_stop("not-a-number", market="/MES")
        assert _extract_stop_price_from_sizing_trace(trace, market="/MES") is None


# ---------------------------------------------------------------------------
# plan_replace_protective_stop
# ---------------------------------------------------------------------------


class TestPlanReplaceProtectiveStopLong:
    """Long position → sell-stop below avg_cost."""

    def test_uses_sizing_trace_when_no_override(self) -> None:
        plan = plan_replace_protective_stop(
            position=_long_position(qty=3, avg_cost=Decimal("4500.00")),
            trade=_trade(),
            sizing_trace=_sizing_trace_with_stop("4450.00", market="/MES"),
            stop_price_override=None,
            env="paper",
        )
        assert plan.stop_side == "sell"
        assert plan.stop_quantity == Decimal(3)
        assert plan.stop_price == Decimal("4450.00")
        assert plan.stop_source == "sizing_trace"
        assert plan.prior_position_direction == "long"

    def test_uses_override_when_provided(self) -> None:
        plan = plan_replace_protective_stop(
            position=_long_position(qty=3, avg_cost=Decimal("4500.00")),
            trade=_trade(),
            sizing_trace=_sizing_trace_with_stop("4450.00", market="/MES"),
            stop_price_override=Decimal("4400.00"),
            env="paper",
        )
        assert plan.stop_price == Decimal("4400.00")
        assert plan.stop_source == "operator_override"

    def test_stop_tick_rounded(self) -> None:
        # /MES tick = 0.25; 4450.30 should round to nearest 0.25 grid
        plan = plan_replace_protective_stop(
            position=_long_position(qty=3, avg_cost=Decimal("4500.00")),
            trade=_trade(),
            sizing_trace={},
            stop_price_override=Decimal("4450.30"),
            env="paper",
        )
        assert (plan.stop_price % Decimal("0.25")) == Decimal(0)

    def test_stop_above_avg_cost_raises(self) -> None:
        """Long position, stop at OR ABOVE avg_cost → would fire immediately."""
        with pytest.raises(ReplaceProtectiveStopError) as exc_info:
            plan_replace_protective_stop(
                position=_long_position(qty=3, avg_cost=Decimal("4500.00")),
                trade=_trade(),
                sizing_trace={},
                stop_price_override=Decimal("4600.00"),  # ABOVE
                env="paper",
            )
        assert exc_info.value.error_code == "STOP_DIRECTION_INVALID"

    def test_stop_equal_to_avg_cost_raises(self) -> None:
        with pytest.raises(ReplaceProtectiveStopError) as exc_info:
            plan_replace_protective_stop(
                position=_long_position(qty=3, avg_cost=Decimal("4500.00")),
                trade=_trade(),
                sizing_trace={},
                stop_price_override=Decimal("4500.00"),  # EQUAL
                env="paper",
            )
        assert exc_info.value.error_code == "STOP_DIRECTION_INVALID"


class TestPlanReplaceProtectiveStopShort:
    """Short position → buy-stop above avg_cost."""

    def test_short_position_uses_buy_stop(self) -> None:
        plan = plan_replace_protective_stop(
            position=_short_position(qty=-2, avg_cost=Decimal("4400.00")),
            trade=_trade(),
            sizing_trace={},
            stop_price_override=Decimal("4500.00"),
            env="paper",
        )
        assert plan.stop_side == "buy"
        assert plan.stop_quantity == Decimal(2)
        assert plan.prior_position_direction == "short"

    def test_short_stop_below_avg_cost_raises(self) -> None:
        with pytest.raises(ReplaceProtectiveStopError) as exc_info:
            plan_replace_protective_stop(
                position=_short_position(qty=-2, avg_cost=Decimal("4400.00")),
                trade=_trade(),
                sizing_trace={},
                stop_price_override=Decimal("4300.00"),  # BELOW
                env="paper",
            )
        assert exc_info.value.error_code == "STOP_DIRECTION_INVALID"


class TestPlanReplaceProtectiveStopFlatPosition:
    def test_zero_quantity_raises(self) -> None:
        flat_position = OpenPositionSnapshot(
            position_id=_POSITION_ID,
            account_id=_ACCOUNT_ID,
            market="/MES",
            contract_id=None,
            quantity=0,
            avg_cost=Decimal("4500.00"),
        )
        with pytest.raises(ReplaceProtectiveStopError) as exc_info:
            plan_replace_protective_stop(
                position=flat_position,
                trade=_trade(),
                sizing_trace=_sizing_trace_with_stop("4450.00"),
                stop_price_override=None,
                env="paper",
            )
        assert exc_info.value.error_code == "POSITION_FLAT"


class TestPlanReplaceProtectiveStopNoStopPriceAvailable:
    def test_missing_sizing_trace_no_override_raises(self) -> None:
        with pytest.raises(ReplaceProtectiveStopError) as exc_info:
            plan_replace_protective_stop(
                position=_long_position(),
                trade=_trade(),
                sizing_trace={},
                stop_price_override=None,
                env="paper",
            )
        assert exc_info.value.error_code == "STOP_PRICE_UNAVAILABLE"


class TestPlanReplaceProtectiveStopUnknownMarket:
    def test_unknown_market_raises(self) -> None:
        unknown_position = OpenPositionSnapshot(
            position_id=_POSITION_ID,
            account_id=_ACCOUNT_ID,
            market="/UNKNOWN_MARKET_XYZ",
            contract_id=None,
            quantity=2,
            avg_cost=Decimal("100.00"),
        )
        with pytest.raises(ReplaceProtectiveStopError) as exc_info:
            plan_replace_protective_stop(
                position=unknown_position,
                trade=_trade(),
                sizing_trace={},
                stop_price_override=Decimal("90.00"),
                env="paper",
            )
        assert exc_info.value.error_code == "UNKNOWN_MARKET_TICK"


class TestPlanReplaceProtectiveStopParentLinkage:
    """The plan's parent_entry_order_id must chain to the original entry
    order id (NOT the cancelled bracket-stop)."""

    def test_plan_chains_to_entry_order(self) -> None:
        plan = plan_replace_protective_stop(
            position=_long_position(),
            trade=_trade(),
            sizing_trace=_sizing_trace_with_stop("2800.00"),
            stop_price_override=None,
            env="paper",
        )
        assert plan.entry_order_id == _ENTRY_ORDER_ID
        assert plan.entry_signal_id == _ENTRY_SIGNAL_ID


# ---------------------------------------------------------------------------
# _broker_status_to_orders_status (mirror of worker helper)
# ---------------------------------------------------------------------------


class TestBrokerStatusMapping:
    @pytest.mark.parametrize(
        "broker,expected",
        [
            ("submitted", "working"),
            ("working", "working"),
            ("pending_submit", "pending"),
            ("rejected", "rejected"),
            ("filled", "filled"),
            ("partially_filled", "partially_filled"),
            ("cancelled", "cancelled"),
            ("expired", "expired"),
            ("UnknownStatusFromIbkr", "pending"),
        ],
    )
    def test_mapping(self, broker: str, expected: str) -> None:
        assert _broker_status_to_orders_status(broker) == expected


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_minimal_args_dry_run_default(self) -> None:
        args = _parse_args(["--market", "/MES", "--env", "paper"])
        assert args.market == "/MES"
        assert args.env == "paper"
        assert args.dry_run is True
        assert args.confirm is False
        assert args.force is False
        assert args.stop_price_override is None
        assert args.account_id_override is None
        assert args.client_id == 99

    def test_no_dry_run_without_confirm_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "/MES", "--env", "paper", "--no-dry-run"])

    def test_no_dry_run_with_confirm_ok(self) -> None:
        args = _parse_args(["--market", "/MES", "--env", "paper", "--no-dry-run", "--confirm"])
        assert args.dry_run is False
        assert args.confirm is True

    def test_stop_price_override_parsed(self) -> None:
        args = _parse_args(["--market", "/MES", "--env", "paper", "--stop-price", "2750.20"])
        assert args.stop_price_override == Decimal("2750.20")

    def test_invalid_stop_price_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "/MES", "--env", "paper", "--stop-price", "abc"])

    def test_negative_stop_price_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "/MES", "--env", "paper", "--stop-price", "-50"])

    def test_account_override_uuid_parsed(self) -> None:
        valid_uuid = "12345678-1234-1234-1234-123456789012"
        args = _parse_args(["--market", "/MES", "--env", "paper", "--account", valid_uuid])
        assert args.account_id_override == UUID(valid_uuid)

    def test_invalid_account_uuid_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "/MES", "--env", "paper", "--account", "not-a-uuid"])

    def test_live_env_requires_allow_non_paper(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "/MES", "--env", "live-small"])

    def test_live_env_with_allow_non_paper_ok(self) -> None:
        args = _parse_args(["--market", "/MES", "--env", "live-small", "--allow-non-paper"])
        assert args.env == "live-small"
        assert args.allow_non_paper is True

    def test_client_id_below_range_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "/MES", "--env", "paper", "--client-id", "1"])

    def test_client_id_above_range_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "/MES", "--env", "paper", "--client-id", "100"])

    def test_client_id_in_range_ok(self) -> None:
        args = _parse_args(["--market", "/MES", "--env", "paper", "--client-id", "85"])
        assert args.client_id == 85

    def test_force_flag(self) -> None:
        args = _parse_args(["--market", "/MES", "--env", "paper", "--force"])
        assert args.force is True

    def test_empty_market_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--market", "   ", "--env", "paper"])


# ---------------------------------------------------------------------------
# main() exit-code contract on argparse errors
# ---------------------------------------------------------------------------


class TestMainExitCodes:
    def test_main_returns_bad_args_on_missing_required(self) -> None:
        # Missing --market
        code = main(["--env", "paper"])
        assert code == EXIT_BAD_ARGS

    def test_main_help_returns_ok(self, capsys: pytest.CaptureFixture[str]) -> None:
        # --help triggers argparse's natural exit(0); main() catches the
        # SystemExit + routes to EXIT_OK on exc.code == 0.
        code = main(["--help"])
        assert code == 0
        # Help text was printed
        captured = capsys.readouterr()
        assert "replace_protective_stop" in captured.out


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_public_surface(self) -> None:
        from scripts.operator_tools import replace_protective_stop

        for name in (
            "OpenPositionSnapshot",
            "OpenTradeSnapshot",
            "ParsedArgs",
            "ReplaceProtectiveStopError",
            "ReplaceProtectiveStopPlan",
            "apply_replace_protective_stop",
            "fetch_active_account_id",
            "fetch_existing_working_bracket_stop",
            "fetch_open_position",
            "fetch_open_trade",
            "fetch_sizing_trace",
            "main",
            "plan_replace_protective_stop",
        ):
            assert hasattr(replace_protective_stop, name), f"missing {name}"

    def test_exit_codes_are_unique(self) -> None:
        from scripts.operator_tools import replace_protective_stop as rps

        codes = {
            rps.EXIT_OK,
            rps.EXIT_NO_POSITION,
            rps.EXIT_BRACKET_ALREADY_PROTECTED,
            rps.EXIT_DIRECTION_SANITY,
            rps.EXIT_IBKR_CONNECT_FAILED,
            rps.EXIT_DB_INIT_FAILED,
            rps.EXIT_BAD_ARGS,
            rps.EXIT_CONFIRM_MISSING,
            rps.EXIT_BROKER_REJECTED,
            rps.EXIT_UNEXPECTED,
        }
        # 10 distinct codes
        assert len(codes) == 10

    def test_plan_dataclass_frozen(self) -> None:
        plan = plan_replace_protective_stop(
            position=_long_position(),
            trade=_trade(),
            sizing_trace=_sizing_trace_with_stop("2800.00"),
            stop_price_override=None,
            env="paper",
        )
        # Frozen dataclass — assignment should raise
        with pytest.raises(Exception):
            plan.stop_price = Decimal("999")  # type: ignore[misc]
