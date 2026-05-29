"""Unit tests for :mod:`services.reconciliation.ibkr_intraday`.

PR-A of Option C (2026-05-28). Pure adapter-mocking — no IBKR gateway, no
Postgres. The new ``fetch_recon_positions`` owns a per-cycle connect →
``get_positions`` → disconnect against the ``IbkrClient`` Protocol; these tests
inject a ``MagicMock`` client (the canonical shape from
``tests/integration/test_replace_protective_stop_end_to_end.py::_fake_ibkr_client``)
to drive every branch: happy path, empty result, connect failure, mid-fetch
failure, disconnect failure, symbol normalization, and the clientId=4 default.

``asyncio_mode = "auto"`` (pyproject) — async tests need no decorator.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from services.execution.types import (
    IbkrConnectionState,
    IbkrContractRef,
    IbkrPlacementError,
    IbkrPosition,
)
from services.reconciliation.ibkr_intraday import (
    DEFAULT_RECON_CLIENT_ID,
    ReconPosition,
    ReconPositionsFetchError,
    fetch_recon_positions,
)


def _ibkr_position(*, market: str, quantity: Decimal) -> IbkrPosition:
    """Build an :class:`IbkrPosition` for a market + signed quantity.

    Only ``contract.market`` + ``quantity`` are read by the module under
    test; the MTM / cost fields are filled with inert values so the frozen
    dataclass constructs. ``exchange`` follows the futures-vs-ETF convention
    (cosmetic here — the adapter, not this module, sets ``market``).
    """
    return IbkrPosition(
        contract=IbkrContractRef(
            market=market,
            ibkr_local_symbol=market.lstrip("/"),
            ibkr_con_id=None,
            multiplier=Decimal("1"),
            exchange="CME" if market.startswith("/") else "SMART",
        ),
        quantity=quantity,
        avg_cost_usd=Decimal("100"),
        market_price_usd=None,
        unrealized_pnl_usd=None,
        realized_pnl_usd=Decimal("0"),
    )


def _placement_error(*, operation: str = "connect") -> IbkrPlacementError:
    """Build the adapter's terminal-failure exception for a given operation."""
    return IbkrPlacementError(
        operation=operation,
        detail=f"{operation} failed: ConnectionError()",
        underlying_exception_class="ConnectionError",
        occurred_at_utc=datetime.now(tz=UTC),
    )


def _fake_client(
    *,
    positions: list[IbkrPosition] | None = None,
    connect_raises: BaseException | None = None,
    get_positions_raises: BaseException | None = None,
    disconnect_raises: BaseException | None = None,
) -> MagicMock:
    """Construct a MagicMock IbkrClient with controllable async behavior.

    Mirrors the ``_fake_ibkr_client`` shape: ``AsyncMock`` per coroutine
    method, with ``side_effect`` for the failure injections. ``connect``
    returns an inert :class:`IbkrConnectionState` (the module ignores the
    return value) unless ``connect_raises`` is set.
    """
    client = MagicMock()
    if connect_raises is not None:
        client.connect = AsyncMock(side_effect=connect_raises)
    else:
        client.connect = AsyncMock(
            return_value=IbkrConnectionState(
                is_connected=True,
                last_error=None,
                client_id=DEFAULT_RECON_CLIENT_ID,
                server_version=176,
            )
        )
    if get_positions_raises is not None:
        client.get_positions = AsyncMock(side_effect=get_positions_raises)
    else:
        client.get_positions = AsyncMock(return_value=positions or [])
    if disconnect_raises is not None:
        client.disconnect = AsyncMock(side_effect=disconnect_raises)
    else:
        client.disconnect = AsyncMock(return_value=None)
    return client


class TestFetchReconPositions:
    """Connect → get_positions → disconnect lifecycle + failure handling."""

    async def test_returns_positions_on_happy_path(self) -> None:
        client = _fake_client(
            positions=[
                _ibkr_position(market="/MES", quantity=Decimal("2")),
                _ibkr_position(market="TLT", quantity=Decimal("-3")),
            ]
        )

        result = await fetch_recon_positions(client_factory=lambda: client)

        assert result == (
            ReconPosition(market="/MES", quantity=Decimal("2")),
            ReconPosition(market="TLT", quantity=Decimal("-3")),
        )
        # Quantity preserved as Decimal (A05), not coerced to int/float.
        assert isinstance(result[0].quantity, Decimal)
        client.connect.assert_awaited_once()
        client.get_positions.assert_awaited_once()
        client.disconnect.assert_awaited_once()

    async def test_returns_empty_tuple_when_no_positions(self) -> None:
        client = _fake_client(positions=[])

        result = await fetch_recon_positions(client_factory=lambda: client)

        assert result == ()
        client.connect.assert_awaited_once()
        client.disconnect.assert_awaited_once()

    async def test_connect_failure_raises_recon_fetch_error(self) -> None:
        underlying = _placement_error(operation="connect")
        client = _fake_client(connect_raises=underlying)

        with pytest.raises(ReconPositionsFetchError) as exc_info:
            await fetch_recon_positions(client_factory=lambda: client)

        assert exc_info.value.operation == "connect"
        assert exc_info.value.underlying_exception_class == "ConnectionError"
        # Original adapter exception preserved on the chain.
        assert exc_info.value.__cause__ is underlying
        # Never reached the fetch; nothing to disconnect (connect failed).
        client.get_positions.assert_not_awaited()
        client.disconnect.assert_not_awaited()

    async def test_get_positions_failure_raises_then_disconnects(self) -> None:
        underlying = _placement_error(operation="reqPositions")
        client = _fake_client(get_positions_raises=underlying)

        with pytest.raises(ReconPositionsFetchError) as exc_info:
            await fetch_recon_positions(client_factory=lambda: client)

        assert exc_info.value.operation == "reqPositions"
        assert exc_info.value.__cause__ is underlying
        # The finally block still disconnects the session we opened.
        client.connect.assert_awaited_once()
        client.disconnect.assert_awaited_once()

    async def test_disconnect_failure_is_swallowed_and_logged(self) -> None:
        client = _fake_client(
            positions=[_ibkr_position(market="/MES", quantity=Decimal("1"))],
            disconnect_raises=RuntimeError("socket already closed"),
        )

        with capture_logs() as logs:
            result = await fetch_recon_positions(client_factory=lambda: client)

        # The fetched result is returned despite the disconnect blowing up.
        assert result == (ReconPosition(market="/MES", quantity=Decimal("1")),)
        events = {entry["event"] for entry in logs}
        assert "recon_positions_disconnect_failed" in events
        # The disconnect failure is NOT re-raised as a fetch error.
        assert "recon_positions_fetch_completed" in events


class TestSymbolNormalization:
    """The adapter pre-normalizes ``market``; this module passes it through."""

    async def test_futures_get_leading_slash(self) -> None:
        client = _fake_client(positions=[_ibkr_position(market="/MES", quantity=Decimal("1"))])

        result = await fetch_recon_positions(client_factory=lambda: client)

        assert result == (ReconPosition(market="/MES", quantity=Decimal("1")),)

    async def test_etfs_stay_unprefixed(self) -> None:
        client = _fake_client(positions=[_ibkr_position(market="TLT", quantity=Decimal("10"))])

        result = await fetch_recon_positions(client_factory=lambda: client)

        assert result == (ReconPosition(market="TLT", quantity=Decimal("10")),)

    async def test_zero_quantity_positions_dropped(self) -> None:
        client = _fake_client(
            positions=[
                _ibkr_position(market="/MES", quantity=Decimal("1")),
                _ibkr_position(market="TLT", quantity=Decimal("0")),
                _ibkr_position(market="/M2K", quantity=Decimal("-2")),
            ]
        )

        result = await fetch_recon_positions(client_factory=lambda: client)

        markets = [pos.market for pos in result]
        assert markets == ["/MES", "/M2K"]
        assert all(pos.quantity != 0 for pos in result)


class TestStructlogDiscipline:
    """Lifecycle log lines carry the canonical recon fields."""

    async def test_started_and_completed_carry_service_and_client_id(self) -> None:
        client = _fake_client(positions=[_ibkr_position(market="/MES", quantity=Decimal("1"))])

        with capture_logs() as logs:
            await fetch_recon_positions(account_id="U25655583", client_factory=lambda: client)

        by_event = {entry["event"]: entry for entry in logs}
        assert "recon_positions_fetch_started" in by_event
        assert "recon_positions_fetch_completed" in by_event
        started = by_event["recon_positions_fetch_started"]
        assert started["service_name"] == "reconciliation"
        assert started["client_id"] == DEFAULT_RECON_CLIENT_ID
        assert started["account_id"] == "U25655583"
        completed = by_event["recon_positions_fetch_completed"]
        assert completed["positions_count"] == 1
        assert completed["raw_count"] == 1

    async def test_fetch_failed_logged_with_operation(self) -> None:
        client = _fake_client(get_positions_raises=_placement_error(operation="reqPositions"))

        with capture_logs() as logs, pytest.raises(ReconPositionsFetchError):
            await fetch_recon_positions(client_factory=lambda: client)

        failed = [e for e in logs if e["event"] == "recon_positions_fetch_failed"]
        assert len(failed) == 1
        assert failed[0]["operation"] == "reqPositions"
        assert failed[0]["underlying_exception_class"] == "ConnectionError"


class TestModuleContract:
    """Locks the load-bearing constants other code + docs depend on."""

    def test_default_recon_client_id_is_4(self) -> None:
        # If this changes, update dev-guide §1.5 LOCKED clientId allocation
        # (collision risk — see module docstring + R2 in the design guide).
        assert DEFAULT_RECON_CLIENT_ID == 4

    def test_recon_position_is_frozen(self) -> None:
        pos = ReconPosition(market="/MES", quantity=Decimal("1"))
        with pytest.raises((AttributeError, TypeError)):
            pos.quantity = Decimal("2")  # type: ignore[misc]


class TestDefaultClientFactory:
    """When no factory is injected, a real adapter is built on clientId=4."""

    async def test_default_factory_passes_recon_client_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_ctor(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            client = MagicMock()
            client.connect = AsyncMock()
            client.get_positions = AsyncMock(return_value=[])
            client.disconnect = AsyncMock()
            return client

        monkeypatch.setattr("services.reconciliation.ibkr_intraday.IbAsyncIbkrClient", _fake_ctor)

        result = await fetch_recon_positions(account_id="U25655583")

        assert result == ()
        assert captured["client_id"] == DEFAULT_RECON_CLIENT_ID
        assert captured["account_id"] == "U25655583"
        assert captured["host"] == "ib_gateway"
        assert captured["port"] == 4004
        assert captured["connect_timeout_seconds"] == 30.0
