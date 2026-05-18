"""Unit tests for ``scripts/operator_tools/replay_executions.py``.

A22 binds: tests exercise argparse + aggregation math + exit-code +
error handling against fake ``ib_async.IB`` + fake
``process_fill_event``. Zero testcontainers, zero real IBKR
connection. The script's docstring documents this contract.

Coverage matrix:

* Argparse — every CLI arg, every default, required-flag behavior, the
  --allow-non-paper gate on live-env writes, the empty
  --client-order-ids guard.
* Aggregation math (VWAP / commission / latest-time) parametrized
  across 1 / 3 / 5-fill cases. Locks the contract against future
  off-by-one / float-coercion drift.
* Empty filter result → exit code 1 + structured log
  ``replay_executions_no_match`` per CID.
* process_fill_event raises UnsupportedFillScenarioError → exit 2.
* process_fill_event raises FillProcessingError → exit 3.
* process_fill_event returns None → logged WARNING, no fail (exit 0).
* Happy path: 2 CIDs both processed → exit 0, both audit_event_uuids
  logged.
* IBKR connection failure → exit 4.
* DB pool init failure (DATABASE_URL missing) → exit 5.

Implementation strategy:

* Build ``_FakeIB`` factory mirroring the ``ib_async.IB`` surface our
  script actually touches: ``connectAsync``, ``isConnected``,
  ``disconnect``, ``reqExecutionsAsync``.
* Build ``_FakeFill`` / ``_FakeExecution`` / ``_FakeCommissionReport``
  namedtuple-style stubs so the aggregation code can read
  ``fill.execution.orderRef`` / ``fill.execution.shares`` /
  ``fill.execution.price`` / ``fill.time`` /
  ``fill.commissionReport.commission``.
* Build ``_FakeProcessFillEvent`` to inject as
  ``process_fill_event_override`` — supports happy path, raise on
  call-N, return None. Carries the stub error classes via dunder
  attributes per the script's contract.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import structlog

# Ensure the script is importable as ``scripts.operator_tools.replay_executions``.
# Avoiding the project's normal install convention because the script is
# a CLI module rather than a service. The test harness imports it via
# the project's PYTHONPATH (provided by `pytest`'s sys.path insertion
# from rootdir).


# ---------------------------------------------------------------------------
# Test doubles: fake IBKR Fill / Execution / CommissionReport
# ---------------------------------------------------------------------------


@dataclass
class _FakeCommissionReport:
    commission: float


@dataclass
class _FakeExecution:
    orderRef: str
    shares: Decimal
    price: float
    side: str = "BOT"
    acctNumber: str = "U25655583"
    permId: int = 100
    clientId: int = 1
    orderId: int = 1


@dataclass
class _FakeFill:
    execution: _FakeExecution
    commissionReport: _FakeCommissionReport
    time: datetime
    # The ib_async Fill has a ``contract`` too but our script doesn't
    # read it; omit for brevity.


def _make_fill(
    *,
    order_ref: str,
    shares: float,
    price: float,
    commission: float,
    time: datetime,
) -> _FakeFill:
    return _FakeFill(
        execution=_FakeExecution(
            orderRef=order_ref,
            shares=Decimal(str(shares)),
            price=price,
        ),
        commissionReport=_FakeCommissionReport(commission=commission),
        time=time,
    )


# ---------------------------------------------------------------------------
# Test doubles: fake ib_async.IB + ib_async module shim
# ---------------------------------------------------------------------------


class _FakeIB:
    """Minimal stand-in for ib_async.IB.

    Implements just connectAsync / isConnected / disconnect /
    reqExecutionsAsync since those are the only methods the script
    invokes. The fake's behavior is steered by ``connect_raises`` +
    ``executions_to_return`` attributes assigned by the test.
    """

    def __init__(self) -> None:
        self._connected = False
        self.connect_raises: Exception | None = None
        self.executions_to_return: list[_FakeFill] = []
        self.connect_calls: list[dict[str, Any]] = []
        self.req_calls: list[Any] = []

    async def connectAsync(self, **kwargs: Any) -> None:
        self.connect_calls.append(kwargs)
        if self.connect_raises is not None:
            raise self.connect_raises
        self._connected = True

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    async def reqExecutionsAsync(self, exec_filter: Any) -> list[_FakeFill]:
        self.req_calls.append(exec_filter)
        return list(self.executions_to_return)


@dataclass
class _FakeExecutionFilter:
    """Mimics the ib_async.ExecutionFilter — just stores kwargs."""

    acctCode: str | None = None
    time: str | None = None


def _install_fake_ib_async_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Insert a fake ``ib_async`` module into sys.modules so the script's
    lazy imports resolve to our test doubles.

    Returns the installed module so tests can inspect / mutate the IB +
    ExecutionFilter symbols.
    """
    fake_module = ModuleType("ib_async")
    fake_module.IB = _FakeIB  # type: ignore[attr-defined]
    fake_module.ExecutionFilter = _FakeExecutionFilter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_module)
    return fake_module


# ---------------------------------------------------------------------------
# Test doubles: fake fill_processor error classes + process_fill_event
# ---------------------------------------------------------------------------


class _StubFillProcessingError(Exception):
    def __init__(
        self, error_code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


class _StubUnsupportedScenarioError(_StubFillProcessingError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("FILL_EXIT_CASE_DEFERRED", message, details)


def _make_process_fill_event_stub(
    *,
    side_effects: list[Any],
) -> Callable[..., Awaitable[Any]]:
    """Return a stub ``process_fill_event`` that walks ``side_effects``
    in order — each entry is either a value to return, or an exception
    instance to raise.

    The script reads ``__fill_processing_error_cls__`` /
    ``__unsupported_scenario_error_cls__`` dunder attrs off the
    callable to know which exception classes to catch; we attach the
    stub classes here so the test path mirrors production semantics.
    """
    call_count = {"n": 0}

    async def _stub(**kwargs: Any) -> Any:
        idx = call_count["n"]
        call_count["n"] += 1
        if idx >= len(side_effects):
            raise AssertionError(
                f"process_fill_event stub called more times ({idx + 1}) than "
                f"side_effects provided ({len(side_effects)})"
            )
        effect = side_effects[idx]
        if isinstance(effect, BaseException):
            raise effect
        return effect

    _stub.__fill_processing_error_cls__ = _StubFillProcessingError  # type: ignore[attr-defined]
    _stub.__unsupported_scenario_error_cls__ = _StubUnsupportedScenarioError  # type: ignore[attr-defined]
    _stub.call_count = call_count  # type: ignore[attr-defined]
    return _stub


def _make_fill_apply_result(*, audit_event_uuid: UUID | None = None) -> SimpleNamespace:
    """Build a ``FillApplyResult``-shaped namespace for the success path."""
    uuid_val = audit_event_uuid or UUID("019e36ab-a998-78ae-b8cb-82ceefc5201b")
    return SimpleNamespace(
        audit_event_uuids=(uuid_val,),
        signal_id=UUID("019e36b1-1111-78ae-b8cb-82ceefc5201b"),
        account_id=UUID("019e36b1-2222-78ae-b8cb-82ceefc5201b"),
        fill_id=UUID("019e36b1-3333-78ae-b8cb-82ceefc5201b"),
        position_id=UUID("019e36b1-4444-78ae-b8cb-82ceefc5201b"),
        trade_id=UUID("019e36b1-5555-78ae-b8cb-82ceefc5201b"),
        balance_id=UUID("019e36b1-6666-78ae-b8cb-82ceefc5201b"),
        new_order_status="filled",
    )


# ---------------------------------------------------------------------------
# Shared module import + autouse setup
# ---------------------------------------------------------------------------


@pytest.fixture
def script_module() -> ModuleType:
    """Import the script fresh per-test so monkeypatched sys.modules
    state can't leak across tests.
    """
    # Clear any prior import so the lazy ib_async resolution sees the
    # current monkeypatched module.
    sys.modules.pop("scripts.operator_tools.replay_executions", None)
    import scripts.operator_tools.replay_executions as mod

    return mod


@pytest.fixture(autouse=True)
def _reset_structlog_capture() -> None:
    """Ensure structlog is configured to use the default (PrintLogger
    via stderr) — tests use ``structlog.testing.capture_logs`` to
    capture events when they need to assert on log content.
    """
    structlog.reset_defaults()


# ===========================================================================
# 1. Argparse
# ===========================================================================


class TestArgparse:
    """Cover every CLI arg + the two custom validation gates."""

    def test_required_args_missing_raises(self, script_module: ModuleType) -> None:
        with pytest.raises(SystemExit):
            script_module._parse_args([])

    def test_required_client_order_ids_only(self, script_module: ModuleType) -> None:
        with pytest.raises(SystemExit):
            script_module._parse_args(["--env", "paper"])

    def test_required_env_only(self, script_module: ModuleType) -> None:
        with pytest.raises(SystemExit):
            script_module._parse_args(["--client-order-ids", "cid-1"])

    def test_invalid_env_rejected(
        self, script_module: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            script_module._parse_args(
                [
                    "--client-order-ids",
                    "cid-1",
                    "--env",
                    "production",
                ]
            )
        err = capsys.readouterr().err
        assert "invalid choice" in err
        for env in ("paper", "live-small", "live-scale"):
            assert env in err

    def test_dev_env_not_allowed_for_audit_writes(
        self, script_module: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "dev" is in the verify_chain.py argparse but NOT here because
        # fill_processor's Environment Literal rejects it. Lock that
        # contract.
        with pytest.raises(SystemExit):
            script_module._parse_args(
                [
                    "--client-order-ids",
                    "cid-1",
                    "--env",
                    "dev",
                ]
            )

    def test_defaults(self, script_module: ModuleType) -> None:
        args = script_module._parse_args(
            [
                "--client-order-ids",
                "cid-1",
                "--env",
                "paper",
            ]
        )
        assert args.client_order_ids == ("cid-1",)
        assert args.env == "paper"
        assert args.ibkr_host == script_module.DEFAULT_IBKR_HOST
        assert args.ibkr_port == script_module.DEFAULT_IBKR_PORT
        assert args.client_id == script_module.DEFAULT_CLIENT_ID
        assert args.filter_acct is None
        assert args.filter_time is None
        assert args.dry_run is False
        assert args.allow_non_paper is False

    def test_overrides(self, script_module: ModuleType) -> None:
        args = script_module._parse_args(
            [
                "--client-order-ids",
                "cid-1,cid-2 ,  cid-3",
                "--env",
                "paper",
                "--ibkr-host",
                "127.0.0.1",
                "--ibkr-port",
                "7497",
                "--client-id",
                "42",
                "--filter-acct",
                "U25655583",
                "--filter-time",
                "20260518 00:00:00",
                "--dry-run",
            ]
        )
        # Whitespace strip + empty-element drop.
        assert args.client_order_ids == ("cid-1", "cid-2", "cid-3")
        assert args.ibkr_host == "127.0.0.1"
        assert args.ibkr_port == 7497
        assert args.client_id == 42
        assert args.filter_acct == "U25655583"
        assert args.filter_time == "20260518 00:00:00"
        assert args.dry_run is True

    def test_empty_client_order_ids_after_split_rejected(self, script_module: ModuleType) -> None:
        # Bare commas / whitespace-only input must be rejected, NOT
        # silently parsed as an empty list.
        with pytest.raises(SystemExit):
            script_module._parse_args(
                [
                    "--client-order-ids",
                    ", , ,",
                    "--env",
                    "paper",
                ]
            )

    @pytest.mark.parametrize("env", ["live-small", "live-scale"])
    def test_live_env_requires_allow_non_paper(
        self,
        script_module: ModuleType,
        env: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit):
            script_module._parse_args(
                [
                    "--client-order-ids",
                    "cid-1",
                    "--env",
                    env,
                ]
            )
        err = capsys.readouterr().err
        assert "--allow-non-paper" in err
        assert env in err

    @pytest.mark.parametrize("env", ["live-small", "live-scale"])
    def test_live_env_passes_with_allow_non_paper(
        self, script_module: ModuleType, env: str
    ) -> None:
        args = script_module._parse_args(
            [
                "--client-order-ids",
                "cid-1",
                "--env",
                env,
                "--allow-non-paper",
            ]
        )
        assert args.env == env
        assert args.allow_non_paper is True

    def test_paper_env_does_not_require_allow_non_paper(self, script_module: ModuleType) -> None:
        args = script_module._parse_args(
            [
                "--client-order-ids",
                "cid-1",
                "--env",
                "paper",
            ]
        )
        assert args.allow_non_paper is False


# ===========================================================================
# 2. Aggregation math (VWAP / commission / latest-time)
# ===========================================================================


class TestAggregateFillsForCid:
    """Lock the VWAP + commission-sum + latest-time math against future
    drift. The drill 5 recovery script ran these computations against
    real production fills; any regression here would silently corrupt
    the audit_log's fill_price / commission_usd values.
    """

    def test_single_fill(self, script_module: ModuleType) -> None:
        fill = _make_fill(
            order_ref="cid-1",
            shares=1.0,
            price=85.62,
            commission=1.00,
            time=datetime(2026, 5, 18, 17, 30, 0, tzinfo=UTC),
        )
        result = script_module._aggregate_fills_for_cid(
            client_order_id="cid-1",
            fills=[fill],
        )
        assert result.client_order_id == "cid-1"
        assert result.cumulative_filled_quantity == 1
        assert result.vwap_fill_price == Decimal("85.62000000")
        assert result.total_commission_usd == Decimal("1.00")
        assert result.latest_filled_at_utc == datetime(2026, 5, 18, 17, 30, 0, tzinfo=UTC)

    def test_three_fills_vwap_and_commission_and_latest_time(
        self, script_module: ModuleType
    ) -> None:
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=10,
                price=100.0,
                commission=0.50,
                time=datetime(2026, 5, 18, 17, 30, 0, tzinfo=UTC),
            ),
            _make_fill(
                order_ref="cid-1",
                shares=20,
                price=110.0,
                commission=1.00,
                time=datetime(2026, 5, 18, 17, 31, 0, tzinfo=UTC),
            ),
            _make_fill(
                order_ref="cid-1",
                shares=30,
                price=120.0,
                commission=1.50,
                time=datetime(2026, 5, 18, 17, 29, 0, tzinfo=UTC),
            ),
        ]
        result = script_module._aggregate_fills_for_cid(
            client_order_id="cid-1",
            fills=fills,
        )
        # VWAP = (10*100 + 20*110 + 30*120) / (10+20+30)
        #      = (1000 + 2200 + 3600) / 60
        #      = 6800 / 60 = 113.33333333...
        assert result.cumulative_filled_quantity == 60
        assert result.vwap_fill_price == Decimal("113.33333333")
        assert result.total_commission_usd == Decimal("3.00")
        # Latest is the 17:31 fill, not the 17:30 or 17:29.
        assert result.latest_filled_at_utc == datetime(2026, 5, 18, 17, 31, 0, tzinfo=UTC)

    def test_five_fills_decimal_precision_preserved(self, script_module: ModuleType) -> None:
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.6234,
                commission=0.25,
                time=datetime(2026, 5, 18, 17, 30, i, tzinfo=UTC),
            )
            for i in range(5)
        ]
        # Mix in one different price to test the weighting.
        fills[2] = _make_fill(
            order_ref="cid-1",
            shares=2,
            price=85.6500,
            commission=0.50,
            time=datetime(2026, 5, 18, 17, 30, 2, tzinfo=UTC),
        )
        result = script_module._aggregate_fills_for_cid(
            client_order_id="cid-1",
            fills=fills,
        )
        # 4 fills @ 1 share @ 85.6234 + 1 fill @ 2 shares @ 85.65
        # qty: 4 * 1 + 2 = 6
        # value: 4 * 85.6234 + 2 * 85.65 = 342.4936 + 171.30 = 513.7936
        # vwap = 513.7936 / 6 = 85.63226666...
        assert result.cumulative_filled_quantity == 6
        # Quantized to 8 decimals.
        assert str(result.vwap_fill_price) == "85.63226667"
        # commission: 4 * 0.25 + 0.50 = 1.50
        assert result.total_commission_usd == Decimal("1.50")
        assert result.latest_filled_at_utc == datetime(2026, 5, 18, 17, 30, 4, tzinfo=UTC)

    def test_empty_fills_raises(self, script_module: ModuleType) -> None:
        with pytest.raises(ValueError, match="Cannot aggregate empty"):
            script_module._aggregate_fills_for_cid(
                client_order_id="cid-1",
                fills=[],
            )

    def test_naive_datetime_normalized_to_utc(self, script_module: ModuleType) -> None:
        # ib_async returns tz-aware datetimes in practice but defensively
        # the aggregator stamps UTC on naive datetimes (fill_processor
        # re-validates via A06 so a real-world naive case surfaces
        # cleanly).
        fill = _make_fill(
            order_ref="cid-1",
            shares=1,
            price=85.0,
            commission=1.00,
            time=datetime(2026, 5, 18, 17, 30, 0, tzinfo=UTC),
        )
        fill.time = datetime(2026, 5, 18, 17, 30, 0)  # strip tzinfo
        result = script_module._aggregate_fills_for_cid(
            client_order_id="cid-1",
            fills=[fill],
        )
        assert result.latest_filled_at_utc.tzinfo == UTC

    def test_commission_sentinel_clamped_to_zero(self, script_module: ModuleType) -> None:
        # IBKR returns float_info.max as a sentinel for "not yet computed"
        # in some edge cases; the aggregator clamps to 0 rather than
        # poisoning downstream arithmetic.
        sentinel = sys.float_info.max
        fill = _make_fill(
            order_ref="cid-1",
            shares=1,
            price=85.0,
            commission=sentinel,
            time=datetime(2026, 5, 18, 17, 30, 0, tzinfo=UTC),
        )
        result = script_module._aggregate_fills_for_cid(
            client_order_id="cid-1",
            fills=[fill],
        )
        assert result.total_commission_usd == Decimal("0")


# ===========================================================================
# 3. _group_fills_by_cid
# ===========================================================================


class TestGroupFillsByCid:
    def test_only_wanted_cids_retained(self, script_module: ModuleType) -> None:
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85,
                commission=1,
                time=datetime(2026, 5, 18, tzinfo=UTC),
            ),
            _make_fill(
                order_ref="cid-2",
                shares=1,
                price=85,
                commission=1,
                time=datetime(2026, 5, 18, tzinfo=UTC),
            ),
            _make_fill(
                order_ref="manual-tws",
                shares=1,
                price=85,
                commission=1,
                time=datetime(2026, 5, 18, tzinfo=UTC),
            ),
        ]
        grouped = script_module._group_fills_by_cid(fills=fills, wanted_cids=["cid-1"])
        assert set(grouped.keys()) == {"cid-1"}
        assert len(grouped["cid-1"]) == 1

    def test_missing_cids_get_empty_list(self, script_module: ModuleType) -> None:
        grouped = script_module._group_fills_by_cid(fills=[], wanted_cids=["cid-missing"])
        assert grouped == {"cid-missing": []}

    def test_empty_order_ref_dropped(self, script_module: ModuleType) -> None:
        fill_blank = _make_fill(
            order_ref="",
            shares=1,
            price=85,
            commission=1,
            time=datetime(2026, 5, 18, tzinfo=UTC),
        )
        grouped = script_module._group_fills_by_cid(fills=[fill_blank], wanted_cids=["cid-1"])
        assert grouped == {"cid-1": []}

    def test_multiple_fills_for_same_cid_grouped(self, script_module: ModuleType) -> None:
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85,
                commission=1,
                time=datetime(2026, 5, 18, 17, i, tzinfo=UTC),
            )
            for i in range(3)
        ]
        grouped = script_module._group_fills_by_cid(fills=fills, wanted_cids=["cid-1"])
        assert len(grouped["cid-1"]) == 3


# ===========================================================================
# 4. Exit code computation
# ===========================================================================


class TestComputeExitCode:
    """Precedence: fill_processing_error > unsupported_scenario >
    no_match > none_result > success. Most severe wins.
    """

    def test_all_success(self, script_module: ModuleType) -> None:
        outcomes = [
            script_module.ReplayOutcome(client_order_id="cid-1", kind="success"),
            script_module.ReplayOutcome(client_order_id="cid-2", kind="success"),
        ]
        assert script_module._compute_exit_code(outcomes) == script_module.EXIT_OK

    def test_one_no_match_yields_no_match_code(self, script_module: ModuleType) -> None:
        outcomes = [
            script_module.ReplayOutcome(client_order_id="cid-1", kind="success"),
            script_module.ReplayOutcome(client_order_id="cid-2", kind="no_match"),
        ]
        assert script_module._compute_exit_code(outcomes) == script_module.EXIT_NO_MATCH

    def test_unsupported_scenario_beats_no_match(self, script_module: ModuleType) -> None:
        outcomes = [
            script_module.ReplayOutcome(client_order_id="cid-1", kind="unsupported_scenario"),
            script_module.ReplayOutcome(client_order_id="cid-2", kind="no_match"),
        ]
        assert script_module._compute_exit_code(outcomes) == script_module.EXIT_UNSUPPORTED_SCENARIO

    def test_fill_processing_error_beats_everything(self, script_module: ModuleType) -> None:
        outcomes = [
            script_module.ReplayOutcome(client_order_id="cid-1", kind="fill_processing_error"),
            script_module.ReplayOutcome(client_order_id="cid-2", kind="unsupported_scenario"),
            script_module.ReplayOutcome(client_order_id="cid-3", kind="no_match"),
            script_module.ReplayOutcome(client_order_id="cid-4", kind="success"),
        ]
        assert (
            script_module._compute_exit_code(outcomes) == script_module.EXIT_FILL_PROCESSING_ERROR
        )

    def test_none_result_does_not_fail(self, script_module: ModuleType) -> None:
        # ``none_result`` (process_fill_event returned None — no orders
        # row found) is a warning-only case; the operator investigates
        # but the script does not exit non-zero.
        outcomes = [
            script_module.ReplayOutcome(client_order_id="cid-1", kind="none_result"),
            script_module.ReplayOutcome(client_order_id="cid-2", kind="success"),
        ]
        assert script_module._compute_exit_code(outcomes) == script_module.EXIT_OK

    def test_empty_outcomes_returns_ok(self, script_module: ModuleType) -> None:
        # Edge case: no CIDs supplied (unreachable in practice — argparse
        # gates this — but the function should be total).
        assert script_module._compute_exit_code([]) == script_module.EXIT_OK


# ===========================================================================
# 5. End-to-end _amain (via overrides)
# ===========================================================================


def _build_args(
    script_module: ModuleType,
    *,
    cids: tuple[str, ...] = ("cid-1",),
    env: str = "paper",
    dry_run: bool = False,
) -> Any:
    return script_module.ParsedArgs(
        client_order_ids=cids,
        env=env,
        ibkr_host="ib_gateway",
        ibkr_port=4004,
        client_id=99,
        filter_acct=None,
        filter_time=None,
        dry_run=dry_run,
        allow_non_paper=(env != "paper"),
    )


def _ib_factory_with_fills(fills: list[_FakeFill]) -> tuple[Any, _FakeIB]:
    """Return (factory, captured_ib_instance) — the factory builds the
    instance + sets its canned fills."""
    captured: dict[str, _FakeIB] = {}

    def _factory() -> _FakeIB:
        ib = _FakeIB()
        ib.executions_to_return = list(fills)
        captured["ib"] = ib
        return ib

    return _factory, captured  # type: ignore[return-value]


class TestAmainHappyPath:
    async def test_two_cids_both_processed_returns_exit_ok(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            ),
            _make_fill(
                order_ref="cid-2",
                shares=1,
                price=90.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 31, tzinfo=UTC),
            ),
        ]
        factory, _captured = _ib_factory_with_fills(fills)

        result_a = _make_fill_apply_result(
            audit_event_uuid=UUID("019e36ab-aaaa-78ae-b8cb-82ceefc5201a"),
        )
        result_b = _make_fill_apply_result(
            audit_event_uuid=UUID("019e36ab-bbbb-78ae-b8cb-82ceefc5201b"),
        )
        process_fill_event = _make_process_fill_event_stub(side_effects=[result_a, result_b])

        args = _build_args(script_module, cids=("cid-1", "cid-2"))
        rc = await script_module._amain(
            args,
            ib_factory=factory,
            session_factory_override=AsyncMock(),
            process_fill_event_override=process_fill_event,
        )
        assert rc == script_module.EXIT_OK
        assert process_fill_event.call_count["n"] == 2  # type: ignore[attr-defined]

    async def test_single_cid_audit_event_uuid_logged(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            ),
        ]
        factory, _captured = _ib_factory_with_fills(fills)
        target_uuid = UUID("019e36ab-cccc-78ae-b8cb-82ceefc5201c")
        process_fill_event = _make_process_fill_event_stub(
            side_effects=[_make_fill_apply_result(audit_event_uuid=target_uuid)]
        )

        args = _build_args(script_module, cids=("cid-1",))
        with structlog.testing.capture_logs() as logs:
            rc = await script_module._amain(
                args,
                ib_factory=factory,
                session_factory_override=AsyncMock(),
                process_fill_event_override=process_fill_event,
            )
        assert rc == script_module.EXIT_OK
        replayed_logs = [
            entry
            for entry in logs
            if entry.get("event") == "replay_executions_replayed_successfully"
        ]
        assert len(replayed_logs) == 1
        assert replayed_logs[0]["audit_event_uuid"] == str(target_uuid)
        # The aggregate completion log echoes the same audit_event_uuid.
        completion_logs = [
            entry for entry in logs if entry.get("event") == "replay_executions_completed"
        ]
        assert len(completion_logs) == 1
        assert completion_logs[0]["exit_code"] == script_module.EXIT_OK


class TestAmainNoMatch:
    async def test_cid_with_no_execution_returns_exit_no_match(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        # No fills at all — every CID is a no_match.
        factory, _captured = _ib_factory_with_fills([])
        process_fill_event = _make_process_fill_event_stub(side_effects=[])

        args = _build_args(script_module, cids=("cid-missing",))
        with structlog.testing.capture_logs() as logs:
            rc = await script_module._amain(
                args,
                ib_factory=factory,
                session_factory_override=AsyncMock(),
                process_fill_event_override=process_fill_event,
            )
        assert rc == script_module.EXIT_NO_MATCH
        # ``replay_executions_no_match`` log fired per the contract.
        no_match_logs = [
            entry for entry in logs if entry.get("event") == "replay_executions_no_match"
        ]
        assert len(no_match_logs) == 1
        assert no_match_logs[0]["client_order_id"] == "cid-missing"
        # process_fill_event was never called (no matches).
        assert process_fill_event.call_count["n"] == 0  # type: ignore[attr-defined]

    async def test_partial_match_returns_exit_no_match(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            ),
        ]
        factory, _captured = _ib_factory_with_fills(fills)
        process_fill_event = _make_process_fill_event_stub(side_effects=[_make_fill_apply_result()])

        # cid-1 matches, cid-2 doesn't. Even though cid-1 succeeded,
        # the overall exit is no_match.
        args = _build_args(script_module, cids=("cid-1", "cid-2"))
        rc = await script_module._amain(
            args,
            ib_factory=factory,
            session_factory_override=AsyncMock(),
            process_fill_event_override=process_fill_event,
        )
        assert rc == script_module.EXIT_NO_MATCH
        # cid-1 was processed.
        assert process_fill_event.call_count["n"] == 1  # type: ignore[attr-defined]


class TestAmainErrorPaths:
    async def test_unsupported_scenario_returns_exit_2(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            )
        ]
        factory, _captured = _ib_factory_with_fills(fills)
        process_fill_event = _make_process_fill_event_stub(
            side_effects=[
                _StubUnsupportedScenarioError(
                    "Partial exit fill; deferred to Phase 2+",
                    details={"fill_quantity": 1, "prior_position_quantity": 5},
                )
            ]
        )

        args = _build_args(script_module, cids=("cid-1",))
        rc = await script_module._amain(
            args,
            ib_factory=factory,
            session_factory_override=AsyncMock(),
            process_fill_event_override=process_fill_event,
        )
        assert rc == script_module.EXIT_UNSUPPORTED_SCENARIO

    async def test_fill_processing_error_returns_exit_3(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            )
        ]
        factory, _captured = _ib_factory_with_fills(fills)
        process_fill_event = _make_process_fill_event_stub(
            side_effects=[
                _StubFillProcessingError(
                    error_code="FILL_QUANTITY_NON_POSITIVE",
                    message="fill_quantity=0; must be > 0.",
                )
            ]
        )

        args = _build_args(script_module, cids=("cid-1",))
        rc = await script_module._amain(
            args,
            ib_factory=factory,
            session_factory_override=AsyncMock(),
            process_fill_event_override=process_fill_event,
        )
        assert rc == script_module.EXIT_FILL_PROCESSING_ERROR

    async def test_none_result_logged_warning_no_fail(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            )
        ]
        factory, _captured = _ib_factory_with_fills(fills)
        process_fill_event = _make_process_fill_event_stub(side_effects=[None])

        args = _build_args(script_module, cids=("cid-1",))
        with structlog.testing.capture_logs() as logs:
            rc = await script_module._amain(
                args,
                ib_factory=factory,
                session_factory_override=AsyncMock(),
                process_fill_event_override=process_fill_event,
            )
        # none_result is a warning, not a failure.
        assert rc == script_module.EXIT_OK
        warning_logs = [
            entry for entry in logs if entry.get("event") == "replay_executions_no_orders_row"
        ]
        assert len(warning_logs) == 1

    async def test_ibkr_connect_failure_returns_exit_4(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)

        def _factory_that_raises() -> _FakeIB:
            ib = _FakeIB()
            ib.connect_raises = ConnectionRefusedError("ib_gateway:4004 unreachable")
            return ib

        process_fill_event = _make_process_fill_event_stub(side_effects=[])

        args = _build_args(script_module, cids=("cid-1",))
        rc = await script_module._amain(
            args,
            ib_factory=_factory_that_raises,
            session_factory_override=AsyncMock(),
            process_fill_event_override=process_fill_event,
        )
        assert rc == script_module.EXIT_IBKR_CONNECT_FAILED
        # process_fill_event was never called because we never got
        # past the IBKR connection step.
        assert process_fill_event.call_count["n"] == 0  # type: ignore[attr-defined]

    async def test_db_url_missing_returns_exit_5(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        monkeypatch.delenv(script_module.DATABASE_URL_ENV, raising=False)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            )
        ]
        factory, _captured = _ib_factory_with_fills(fills)
        process_fill_event = _make_process_fill_event_stub(side_effects=[_make_fill_apply_result()])

        # Note: session_factory_override is None so the script tries to
        # build its own from DATABASE_URL.
        args = _build_args(script_module, cids=("cid-1",))
        rc = await script_module._amain(
            args,
            ib_factory=factory,
            session_factory_override=None,
            process_fill_event_override=process_fill_event,
        )
        assert rc == script_module.EXIT_DB_INIT_FAILED


class TestAmainDryRun:
    async def test_dry_run_skips_process_fill_event(
        self,
        script_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_ib_async_module(monkeypatch)
        fills = [
            _make_fill(
                order_ref="cid-1",
                shares=1,
                price=85.0,
                commission=1.0,
                time=datetime(2026, 5, 18, 17, 30, tzinfo=UTC),
            )
        ]
        factory, _captured = _ib_factory_with_fills(fills)
        process_fill_event = _make_process_fill_event_stub(side_effects=[])

        args = _build_args(script_module, cids=("cid-1",), dry_run=True)
        rc = await script_module._amain(
            args,
            ib_factory=factory,
            session_factory_override=None,  # No DB needed for dry-run
            process_fill_event_override=process_fill_event,
        )
        assert rc == script_module.EXIT_OK
        # process_fill_event was NOT called in dry-run.
        assert process_fill_event.call_count["n"] == 0  # type: ignore[attr-defined]


# ===========================================================================
# 6. Module contract
# ===========================================================================


class TestModuleContract:
    def test_exit_codes_locked(self, script_module: ModuleType) -> None:
        # Locked exit-code contract — operator runbook + automation key
        # off these values. Changing them is a runbook-breaking change.
        assert script_module.EXIT_OK == 0
        assert script_module.EXIT_NO_MATCH == 1
        assert script_module.EXIT_UNSUPPORTED_SCENARIO == 2
        assert script_module.EXIT_FILL_PROCESSING_ERROR == 3
        assert script_module.EXIT_IBKR_CONNECT_FAILED == 4
        assert script_module.EXIT_DB_INIT_FAILED == 5
        assert script_module.EXIT_BAD_ARGS == 6
        assert script_module.EXIT_UNEXPECTED == 99

    def test_database_url_env_name_is_bare(self, script_module: ModuleType) -> None:
        # Bare ``DATABASE_URL`` (matches verify_chain.py convention,
        # NOT ``API_DATABASE_URL`` which is the api-process-specific
        # prefixed name).
        assert script_module.DATABASE_URL_ENV == "DATABASE_URL"

    def test_default_client_id_avoids_worker_collision(self, script_module: ModuleType) -> None:
        # Worker uses clientId=1 by default (overridable via PR #167).
        # The script's default MUST be different to avoid kicking the
        # worker off the gateway during a recovery.
        assert script_module.DEFAULT_CLIENT_ID != 1
        assert script_module.DEFAULT_CLIENT_ID == 99

    def test_default_ibkr_port_is_paper(self, script_module: ModuleType) -> None:
        # Default to paper (4004) not live (4002) — safer default for
        # an operator who forgets the --ibkr-port flag.
        assert script_module.DEFAULT_IBKR_PORT == 4004

    def test_all_exports_present(self, script_module: ModuleType) -> None:
        # Lock the public surface of the module.
        for name in (
            "main",
            "_amain",
            "_parse_args",
            "_aggregate_fills_for_cid",
            "_group_fills_by_cid",
            "_compute_exit_code",
            "_replay_one_cid",
            "ParsedArgs",
            "ReplayOutcome",
            "AggregatedExecution",
        ):
            assert name in script_module.__all__, f"missing from __all__: {name}"
