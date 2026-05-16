"""Unit tests for ``services.risk.order_terminal_status_processor``.

Defect #2 fix (2026-05-16): propagate IBKR cancelled / rejected /
inactive orderStatus events to the backend (audit row + UPDATE
orders.status). Tests use fake session_factory + monkeypatched
``append_audit_event`` to validate the pure-policy paths without
testcontainers. Integration test covers the SQL contract end-to-end
elsewhere.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import services.risk.order_terminal_status_processor as ots
from services.risk.order_terminal_status_processor import (
    OrderTerminalStatusError,
    TerminalStatusPayload,
    TerminalStatusResult,
    process_terminal_status_event,
)


def _payload(
    *,
    broker_order_id: int = 99,
    status_kind: str = "rejected",
    rejection_reason: str | None = "321 - Please enter a local symbol or an expiry",
    observed_at_utc: datetime | None = None,
) -> TerminalStatusPayload:
    return TerminalStatusPayload(
        broker_order_id=broker_order_id,
        status_kind=status_kind,  # type: ignore[arg-type]
        rejection_reason=rejection_reason,
        observed_at_utc=observed_at_utc or datetime(2026, 5, 13, 17, 40, 20, tzinfo=UTC),
    )


def _fake_audit_record(sequence_no: int = 8) -> Any:
    rec = MagicMock()
    rec.event_uuid = uuid4()
    rec.sequence_no = sequence_no
    return rec


def _fake_session_factory(
    *,
    order_row: Any = None,
    signal_row: Any = None,
    account_row: Any = None,
) -> Any:
    """Build a fake session_factory that yields async-cm sessions.

    The session's `execute()` returns a fake result whose `fetchone()`
    can be configured per call via a queue of row replies.
    """
    rows: list[Any] = []
    if order_row is not None:
        rows.append(order_row)
    if signal_row is not None:
        rows.append(signal_row)
    if account_row is not None:
        rows.append(account_row)

    @asynccontextmanager
    async def _session_cm() -> Any:
        sess = MagicMock()

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            res = MagicMock()
            # Pop the next row each call
            res.fetchone = MagicMock(return_value=rows.pop(0) if rows else None)
            return res

        sess.execute = _exec
        sess.begin = lambda: _async_null_cm()
        yield sess

    factory = MagicMock()
    factory.side_effect = lambda *a, **k: _session_cm()
    return factory


@asynccontextmanager
async def _async_null_cm() -> Any:
    yield None


class TestProcessTerminalStatusEvent:
    @pytest.mark.asyncio
    async def test_unknown_order_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No order row → returns None silently (logged at WARNING by impl)
        factory = _fake_session_factory(order_row=None)
        result = await process_terminal_status_event(
            session_factory=factory,
            client_order_id="unknown-coid",
            payload=_payload(),
            env="paper",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_already_terminal_returns_none(self) -> None:
        # Order already in 'filled' state → idempotent no-op
        order_row = MagicMock()
        order_row.id = uuid4()
        order_row.signal_id = uuid4()
        order_row.market = "/MNQ"
        order_row.status = "filled"
        order_row.broker_order_id = 99
        factory = _fake_session_factory(order_row=order_row)
        result = await process_terminal_status_event(
            session_factory=factory,
            client_order_id="some-coid",
            payload=_payload(),
            env="paper",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_happy_path_rejected_writes_audit_and_returns_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order_id = uuid4()
        signal_id = uuid4()
        account_id = uuid4()
        order_row = MagicMock()
        order_row.id = order_id
        order_row.signal_id = signal_id
        order_row.market = "/MNQ"
        order_row.status = "pending"
        order_row.broker_order_id = 3
        signal_row = MagicMock()
        signal_row.account_id = account_id

        factory = _fake_session_factory(order_row=order_row, signal_row=signal_row)

        rec = _fake_audit_record(sequence_no=9)
        append_mock = AsyncMock(return_value=rec)
        monkeypatch.setattr(ots, "append_audit_event", append_mock)

        result = await process_terminal_status_event(
            session_factory=factory,
            client_order_id="9d2f7a1c-deadbeef-0",
            payload=_payload(
                broker_order_id=3,
                status_kind="rejected",
                rejection_reason="321 - Please enter a local symbol or an expiry",
            ),
            env="paper",
        )
        assert isinstance(result, TerminalStatusResult)
        assert result.order_id == order_id
        assert result.signal_id == signal_id
        assert result.market == "/MNQ"
        assert result.new_status == "rejected"
        assert result.audit_event_uuid == rec.event_uuid
        assert result.audit_sequence_no == 9

        append_mock.assert_awaited_once()
        # The audit event_type is ORDER_REJECTED
        from services.audit.event_types import AuditEventType

        call_args = append_mock.await_args
        assert call_args.args[1] == AuditEventType.ORDER_REJECTED
        # Audit payload carries the canonical fields
        audit_payload = call_args.args[2]
        assert audit_payload["order_id"] == str(order_id)
        assert audit_payload["broker_order_id"] == 3
        assert audit_payload["new_status"] == "rejected"
        assert audit_payload["rejection_reason"] == "321 - Please enter a local symbol or an expiry"
        assert audit_payload["prior_status"] == "pending"

    @pytest.mark.asyncio
    async def test_cancelled_status_maps_to_order_cancelled_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order_row = MagicMock()
        order_row.id = uuid4()
        order_row.signal_id = uuid4()
        order_row.market = "/MES"
        order_row.status = "pending"
        signal_row = MagicMock()
        signal_row.account_id = uuid4()
        factory = _fake_session_factory(order_row=order_row, signal_row=signal_row)
        append_mock = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(ots, "append_audit_event", append_mock)

        result = await process_terminal_status_event(
            session_factory=factory,
            client_order_id="coid",
            payload=_payload(status_kind="cancelled", rejection_reason="user-cancelled"),
            env="paper",
        )
        assert result is not None
        assert result.new_status == "cancelled"
        from services.audit.event_types import AuditEventType

        assert append_mock.await_args.args[1] == AuditEventType.ORDER_CANCELLED

    @pytest.mark.asyncio
    async def test_inactive_status_maps_to_order_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # IBKR's "Inactive" surfaces validation errors; collapses to
        # the canonical "rejected" wire value.
        order_row = MagicMock()
        order_row.id = uuid4()
        order_row.signal_id = uuid4()
        order_row.market = "/MNQ"
        order_row.status = "pending"
        signal_row = MagicMock()
        signal_row.account_id = uuid4()
        factory = _fake_session_factory(order_row=order_row, signal_row=signal_row)
        append_mock = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(ots, "append_audit_event", append_mock)

        result = await process_terminal_status_event(
            session_factory=factory,
            client_order_id="coid",
            payload=_payload(status_kind="inactive"),
            env="paper",
        )
        assert result is not None
        assert result.new_status == "rejected"
        from services.audit.event_types import AuditEventType

        assert append_mock.await_args.args[1] == AuditEventType.ORDER_REJECTED

    @pytest.mark.asyncio
    async def test_missing_signal_raises_db_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        order_row = MagicMock()
        order_row.id = uuid4()
        order_row.signal_id = uuid4()  # signal_id present
        order_row.market = "/MNQ"
        order_row.status = "pending"
        # But the second fetchone (signals lookup) returns None
        factory = _fake_session_factory(order_row=order_row, signal_row=None)
        with pytest.raises(OrderTerminalStatusError) as exc_info:
            await process_terminal_status_event(
                session_factory=factory,
                client_order_id="coid",
                payload=_payload(),
                env="paper",
            )
        assert exc_info.value.error_code == "DB_ERROR"

    @pytest.mark.asyncio
    async def test_orphan_order_uses_active_account_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Order has no signal_id (manual TWS path); fallback to active account
        account_id = uuid4()
        order_row = MagicMock()
        order_row.id = uuid4()
        order_row.signal_id = None
        order_row.market = "TLT"
        order_row.status = "pending"
        account_row = MagicMock()
        account_row.id = account_id
        # Note: signal_row is skipped because signal_id is None;
        # _fake_session_factory pops rows in order, so we put account_row
        # in the signal_row position (next fetchone consumer is the
        # account fallback query).
        factory = _fake_session_factory(order_row=order_row, signal_row=account_row)
        append_mock = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(ots, "append_audit_event", append_mock)

        result = await process_terminal_status_event(
            session_factory=factory,
            client_order_id="coid",
            payload=_payload(status_kind="cancelled"),
            env="paper",
        )
        assert result is not None
        assert result.signal_id is None
        # Verify the audit fired with the fallback account_id
        call_args = append_mock.await_args
        assert call_args.kwargs.get("account_id") == account_id

    @pytest.mark.asyncio
    async def test_audit_payload_uses_observed_at_utc_as_source_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observed = datetime(2026, 5, 13, 17, 40, 20, 281131, tzinfo=UTC)
        order_row = MagicMock()
        order_row.id = uuid4()
        order_row.signal_id = uuid4()
        order_row.market = "/MNQ"
        order_row.status = "pending"
        signal_row = MagicMock()
        signal_row.account_id = uuid4()
        factory = _fake_session_factory(order_row=order_row, signal_row=signal_row)
        append_mock = AsyncMock(return_value=_fake_audit_record())
        monkeypatch.setattr(ots, "append_audit_event", append_mock)

        await process_terminal_status_event(
            session_factory=factory,
            client_order_id="coid",
            payload=_payload(observed_at_utc=observed),
            env="paper",
        )
        assert append_mock.await_args.kwargs["source_clock_ts"] == observed


class TestPayloadShape:
    def test_terminal_status_payload_is_frozen(self) -> None:
        payload = _payload()
        with pytest.raises(AttributeError):
            payload.broker_order_id = 5  # type: ignore[misc]

    def test_terminal_status_result_is_frozen(self) -> None:
        result = TerminalStatusResult(
            order_id=uuid4(),
            signal_id=uuid4(),
            market="/MNQ",
            new_status="rejected",
            audit_event_uuid=uuid4(),
            audit_sequence_no=1,
        )
        with pytest.raises(AttributeError):
            result.new_status = "filled"  # type: ignore[misc]


class TestModuleContract:
    def test_module_exports(self) -> None:
        from services.risk import order_terminal_status_processor as mod

        assert "process_terminal_status_event" in mod.__all__
        assert "TerminalStatusPayload" in mod.__all__
        assert "TerminalStatusResult" in mod.__all__
        assert "OrderTerminalStatusError" in mod.__all__
        assert "TerminalOrderStatus" in mod.__all__
