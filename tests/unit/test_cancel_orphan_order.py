"""Unit tests for scripts/operator_tools/cancel_orphan_order.py.

Covers the pure safety-classification logic, argparse contract, and the
``_amain`` control flow (not-found / live-at-broker refusal / dry-run /
execute) via a fake session_factory. The actual audit-first mutation is
delegated to the already-tested
``services.risk.order_terminal_status_processor.process_terminal_status_event``
(monkeypatched here), so these tests assert the wrapper's guards + wiring,
not the audit write itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from scripts.operator_tools.cancel_orphan_order import (
    EXIT_LIVE_AT_BROKER,
    EXIT_OK,
    EXIT_ORDER_NOT_FOUND,
    _amain,
    _build_parser,
    classify_order_for_cancellation,
)

# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------


class TestClassifyOrderForCancellation:
    def test_pending_no_broker_id_proceeds(self) -> None:
        assert classify_order_for_cancellation(status="pending", broker_order_id=None) == (
            "proceed",
            EXIT_OK,
        )

    def test_non_pending_non_terminal_no_broker_id_proceeds(self) -> None:
        # classify is status-agnostic for non-terminal values: any
        # non-terminal orders.status with no broker id is a never-placed
        # orphan → safe. 'working' is a valid orders.status CHECK value
        # (pending/working/partially_filled/filled/cancelled/rejected/expired).
        assert classify_order_for_cancellation(status="working", broker_order_id=None) == (
            "proceed",
            EXIT_OK,
        )

    @pytest.mark.parametrize("status", ["filled", "cancelled", "rejected"])
    def test_terminal_status_is_noop(self, status: str) -> None:
        assert classify_order_for_cancellation(status=status, broker_order_id=None) == (
            "already_terminal",
            EXIT_OK,
        )

    def test_broker_order_id_set_refuses_even_if_pending(self) -> None:
        # A live broker order must NOT be DB-cancelled (would desync IBKR).
        assert classify_order_for_cancellation(status="pending", broker_order_id=123) == (
            "live_at_broker",
            EXIT_LIVE_AT_BROKER,
        )

    def test_broker_order_id_zero_is_treated_as_set(self) -> None:
        # broker_order_id=0 is not None → still refuse (defensive; the DB
        # uses NULL for never-placed, so 0 means "something placed it").
        decision, code = classify_order_for_cancellation(status="pending", broker_order_id=0)
        assert decision == "live_at_broker"
        assert code == EXIT_LIVE_AT_BROKER


# ---------------------------------------------------------------------------
# Argparse contract
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_minimal_required_args(self) -> None:
        args = _build_parser().parse_args(["--client-order-id", "cid-1", "--reason", "because"])
        assert args.client_order_id == "cid-1"
        assert args.reason == "because"
        assert args.status_kind == "cancelled"
        assert args.env == "paper"
        assert args.execute is False

    def test_client_order_id_required(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--reason", "x"])

    def test_reason_required(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--client-order-id", "cid-1"])

    def test_execute_and_status_kind_flags(self) -> None:
        args = _build_parser().parse_args(
            ["--client-order-id", "c", "--reason", "r", "--status-kind", "rejected", "--execute"]
        )
        assert args.execute is True
        assert args.status_kind == "rejected"

    def test_invalid_status_kind_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(
                ["--client-order-id", "c", "--reason", "r", "--status-kind", "working"]
            )


# ---------------------------------------------------------------------------
# _amain control flow (fake session_factory)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _FakeSession:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def execute(self, _stmt: Any, _params: Any = None) -> _FakeResult:
        return _FakeResult(self._row)


def _sf_returning(row: Any) -> Any:
    def factory() -> _FakeSession:
        return _FakeSession(row)

    return factory


def _args(**overrides: Any) -> Any:
    argv = [
        "--client-order-id",
        overrides.pop("client_order_id", "cid-X"),
        "--reason",
        overrides.pop("reason", "cleanup"),
    ]
    if overrides.pop("execute", False):
        argv.append("--execute")
    return _build_parser().parse_args(argv)


class TestAmain:
    async def test_order_not_found_returns_not_found_code(self) -> None:
        rc = await _amain(_args(), session_factory_override=_sf_returning(None))
        assert rc == EXIT_ORDER_NOT_FOUND

    async def test_live_at_broker_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"n": 0}

        async def _should_not_call(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
            called["n"] += 1
            return None

        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            _should_not_call,
        )
        row = SimpleNamespace(
            id=uuid4(), signal_id=uuid4(), market="/MYM", status="pending", broker_order_id=42
        )
        rc = await _amain(_args(execute=True), session_factory_override=_sf_returning(row))
        assert rc == EXIT_LIVE_AT_BROKER
        assert called["n"] == 0  # never touched the processor

    async def test_dry_run_proceeds_without_mutation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"n": 0}

        async def _should_not_call(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
            called["n"] += 1
            return None

        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            _should_not_call,
        )
        row = SimpleNamespace(
            id=uuid4(), signal_id=uuid4(), market="/MYM", status="pending", broker_order_id=None
        )
        rc = await _amain(_args(execute=False), session_factory_override=_sf_returning(row))
        assert rc == EXIT_OK
        assert called["n"] == 0  # dry-run never mutates

    async def test_execute_calls_processor_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def _fake_process(*, session_factory, client_order_id, payload, env, phase_at_emit):  # type: ignore[no-untyped-def]
            captured.update(
                client_order_id=client_order_id,
                status_kind=payload.status_kind,
                rejection_reason=payload.rejection_reason,
                env=env,
            )
            return SimpleNamespace(
                order_id=uuid4(),
                new_status="cancelled",
                audit_event_uuid=uuid4(),
                audit_sequence_no=999,
            )

        monkeypatch.setattr(
            "services.risk.order_terminal_status_processor.process_terminal_status_event",
            _fake_process,
        )
        row = SimpleNamespace(
            id=uuid4(), signal_id=uuid4(), market="/MYM", status="pending", broker_order_id=None
        )
        rc = await _amain(
            _args(client_order_id="cid-MYM", reason="orphan cleanup", execute=True),
            session_factory_override=_sf_returning(row),
        )
        assert rc == EXIT_OK
        assert captured["client_order_id"] == "cid-MYM"
        assert captured["status_kind"] == "cancelled"
        assert captured["rejection_reason"] == "orphan cleanup"
        assert captured["env"] == "paper"
