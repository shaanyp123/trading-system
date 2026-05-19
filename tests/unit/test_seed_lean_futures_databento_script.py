"""Unit tests for :mod:`scripts.seed_lean_futures_databento`.

Pure-Python coverage for the per-call timeout helper added 2026-05-19
after a re-seed attempt hung at the DataBento cost-quote step for
~80 minutes without firing any deadline. Tests use ``time.sleep`` +
short real timeouts so the wall-clock contract is exercised end-to-
end (no mocking of ``concurrent.futures`` internals).

The DataBento SDK is intentionally NOT a test dep (it's an
operator-side ceremony-only package per the runbook). The few tests
that exercise ``main()`` substitute a stand-in via ``sys.modules`` so
the test file imports cleanly on a bare CI environment.

Test classes
------------

* :class:`TestSeedCallTimeoutError` — error class shape
* :class:`TestCallWithTimeout` — _call_with_timeout helper contract
* :class:`TestArgparseTimeouts` — new CLI flags
* :class:`TestCostQuoteCombinedTimeout` — cost-quote call propagates
  timeout
* :class:`TestFetchHelpersTimeout` — per-schema fetchers propagate
  timeout
* :class:`TestMainExitCodes` — cost-quote timeout → exit 5;
  per-ticker timeout → exit 2 with informative message
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.seed_lean_futures_databento import (
    DEFAULT_COST_QUOTE_TIMEOUT_SECONDS,
    DEFAULT_PER_CALL_TIMEOUT_SECONDS,
    SeedCallTimeoutError,
    _call_with_timeout,
    cost_quote_combined,
    fetch_definition,
    fetch_ohlcv,
    fetch_open_interest,
    parse_args,
)

# ---------------------------------------------------------------------------
# SeedCallTimeoutError shape
# ---------------------------------------------------------------------------


class TestSeedCallTimeoutError:
    def test_carries_operation_and_timeout(self) -> None:
        err = SeedCallTimeoutError("ohlcv-1d MES", 600.0)
        assert err.operation == "ohlcv-1d MES"
        assert err.timeout_seconds == 600.0
        assert "ohlcv-1d MES" in str(err)
        assert "600" in str(err)

    def test_is_an_exception_subclass(self) -> None:
        err = SeedCallTimeoutError("foo", 1.0)
        assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# _call_with_timeout — uses real ThreadPoolExecutor + real time.sleep
# ---------------------------------------------------------------------------


class TestCallWithTimeout:
    def test_returns_value_on_success(self) -> None:
        result = _call_with_timeout(lambda: 42, timeout_seconds=1.0, operation="test-success")
        assert result == 42

    def test_returns_value_with_args_kwargs(self) -> None:
        def add(a: int, b: int = 0) -> int:
            return a + b

        result = _call_with_timeout(add, 5, b=7, timeout_seconds=1.0, operation="test-args")
        assert result == 12

    def test_timeout_raises_seed_call_timeout_error(self) -> None:
        def slow() -> int:
            time.sleep(2.0)
            return 1

        with pytest.raises(SeedCallTimeoutError) as excinfo:
            _call_with_timeout(slow, timeout_seconds=0.1, operation="test-timeout")
        assert excinfo.value.operation == "test-timeout"
        assert excinfo.value.timeout_seconds == 0.1

    def test_propagates_other_exceptions(self) -> None:
        def boom() -> int:
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            _call_with_timeout(boom, timeout_seconds=1.0, operation="test-error")

    def test_zero_timeout_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds must be > 0"):
            _call_with_timeout(lambda: 1, timeout_seconds=0.0, operation="test-zero")

    def test_negative_timeout_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds must be > 0"):
            _call_with_timeout(lambda: 1, timeout_seconds=-0.5, operation="test-neg")


# ---------------------------------------------------------------------------
# Argparse — new CLI flags
# ---------------------------------------------------------------------------


class TestArgparseTimeouts:
    def test_defaults_match_module_constants(self) -> None:
        args = parse_args(["--yes"])
        assert args.per_call_timeout == DEFAULT_PER_CALL_TIMEOUT_SECONDS
        assert args.cost_quote_timeout == DEFAULT_COST_QUOTE_TIMEOUT_SECONDS

    def test_per_call_timeout_accepts_float(self) -> None:
        args = parse_args(["--yes", "--per-call-timeout", "120.5"])
        assert args.per_call_timeout == 120.5

    def test_cost_quote_timeout_accepts_float(self) -> None:
        args = parse_args(["--yes", "--cost-quote-timeout", "60"])
        assert args.cost_quote_timeout == 60.0


# ---------------------------------------------------------------------------
# cost_quote_combined — timeout propagates as SeedCallTimeoutError
# ---------------------------------------------------------------------------


class TestCostQuoteCombinedTimeout:
    def test_cost_quote_timeout_propagates(self) -> None:
        client = MagicMock()

        def slow_get_cost(**_kwargs: Any) -> float:
            time.sleep(2.0)
            return 0.0

        client.metadata.get_cost = slow_get_cost
        with pytest.raises(SeedCallTimeoutError) as excinfo:
            cost_quote_combined(
                client=client,
                parent_symbols=["MES.FUT"],
                start="2026-05-01",
                end="2026-05-19",
                dataset="GLBX.MDP3",
                timeout_seconds=0.1,
            )
        assert "cost-quote" in excinfo.value.operation

    def test_cost_quote_happy_path(self) -> None:
        client = MagicMock()
        client.metadata.get_cost = MagicMock(return_value=0.5)
        ohlcv, stats, defs, total = cost_quote_combined(
            client=client,
            parent_symbols=["MES.FUT"],
            start="2026-05-01",
            end="2026-05-19",
            dataset="GLBX.MDP3",
            timeout_seconds=1.0,
        )
        assert ohlcv == Decimal("0.5")
        assert stats == Decimal("0.5")
        assert defs == Decimal("0.5")
        assert total == Decimal("1.5")


# ---------------------------------------------------------------------------
# fetch_* helpers — timeout propagates
# ---------------------------------------------------------------------------


class TestFetchHelpersTimeout:
    def test_fetch_definition_timeout_propagates(self) -> None:
        client = MagicMock()

        def slow_get_range(**_kwargs: Any) -> Any:
            time.sleep(2.0)

        client.timeseries.get_range = slow_get_range
        with pytest.raises(SeedCallTimeoutError) as excinfo:
            fetch_definition(
                client,
                "MES.FUT",
                "2026-05-01",
                "2026-05-19",
                "GLBX.MDP3",
                timeout_seconds=0.1,
            )
        assert "definition MES.FUT" in excinfo.value.operation

    def test_fetch_ohlcv_timeout_propagates(self) -> None:
        client = MagicMock()

        def slow_get_range(**_kwargs: Any) -> Any:
            time.sleep(2.0)

        client.timeseries.get_range = slow_get_range
        with pytest.raises(SeedCallTimeoutError) as excinfo:
            fetch_ohlcv(
                client,
                "MES.FUT",
                "2026-05-01",
                "2026-05-19",
                "GLBX.MDP3",
                timeout_seconds=0.1,
            )
        assert "ohlcv-1d MES.FUT" in excinfo.value.operation

    def test_fetch_open_interest_timeout_propagates(self) -> None:
        client = MagicMock()

        def slow_get_range(**_kwargs: Any) -> Any:
            time.sleep(2.0)

        client.timeseries.get_range = slow_get_range
        with pytest.raises(SeedCallTimeoutError) as excinfo:
            fetch_open_interest(
                client,
                "MES.FUT",
                "2026-05-01",
                "2026-05-19",
                "GLBX.MDP3",
                timeout_seconds=0.1,
            )
        assert "statistics MES.FUT" in excinfo.value.operation


# ---------------------------------------------------------------------------
# main() exit codes — drives the script-level integration of the timeout
# helper. Uses sys.modules['databento'] shim because databento isn't a test
# dep + the script imports it lazily inside main().
# ---------------------------------------------------------------------------


class _FakeHistorical:
    """Stand-in for ``databento.Historical``. ``metadata.get_cost`` and
    ``timeseries.get_range`` are MagicMocks the test wires per-case."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.metadata = MagicMock()
        self.timeseries = MagicMock()


class TestMainExitCodes:
    def setup_method(self) -> None:
        # Install a minimal databento stand-in so ``main()``'s
        # ``import databento`` resolves without the real SDK.
        self._db_module = MagicMock()
        self._db_module.Historical = _FakeHistorical
        sys.modules["databento"] = self._db_module

    def teardown_method(self) -> None:
        sys.modules.pop("databento", None)

    def test_cost_quote_timeout_returns_exit_5(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.seed_lean_futures_databento import main

        def slow_get_cost(**_kwargs: Any) -> float:
            time.sleep(2.0)
            return 0.0

        # Patch _FakeHistorical to return a client with a hanging get_cost
        def fake_init(self: Any, key: str) -> None:
            self.key = key
            self.metadata = MagicMock()
            self.metadata.get_cost = slow_get_cost
            self.timeseries = MagicMock()

        monkeypatch.setattr(_FakeHistorical, "__init__", fake_init)

        rc = main(
            [
                "--api-key",
                "dummy",
                "--tickers",
                "MES",
                "--yes",
                "--cost-quote-timeout",
                "0.1",
            ]
        )
        assert rc == 5
        captured = capsys.readouterr()
        assert "cost quote timed out" in captured.err

    def test_per_ticker_timeout_returns_exit_2_with_message(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.seed_lean_futures_databento import main

        def fast_get_cost(**_kwargs: Any) -> float:
            return 0.5

        def slow_get_range(**_kwargs: Any) -> Any:
            time.sleep(2.0)

        def fake_init(self: Any, key: str) -> None:
            self.key = key
            self.metadata = MagicMock()
            self.metadata.get_cost = fast_get_cost
            self.timeseries = MagicMock()
            self.timeseries.get_range = slow_get_range

        monkeypatch.setattr(_FakeHistorical, "__init__", fake_init)

        rc = main(
            [
                "--api-key",
                "dummy",
                "--tickers",
                "MES",
                "--yes",
                "--per-call-timeout",
                "0.1",
            ]
        )
        assert rc == 2
        captured = capsys.readouterr()
        assert "TIMEOUT" in captured.err
        # The aggregate failure message should call out the timeout count
        # and reference the tuneable flag.
        assert "per-call timeout" in captured.err
        assert "--per-call-timeout" in captured.err


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_constants_are_float(self) -> None:
        assert isinstance(DEFAULT_PER_CALL_TIMEOUT_SECONDS, float)
        assert isinstance(DEFAULT_COST_QUOTE_TIMEOUT_SECONDS, float)
        assert DEFAULT_PER_CALL_TIMEOUT_SECONDS > 0
        assert DEFAULT_COST_QUOTE_TIMEOUT_SECONDS > 0
