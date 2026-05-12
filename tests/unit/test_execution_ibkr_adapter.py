"""Unit tests for ``services.execution.ibkr_adapter.IbAsyncIbkrClient``.

Pivot-PR-B (post-pivot 2026-05-12). Tests the adapter against a fake
``ib_async.IB`` instance to avoid touching real ib_async + real TWS API.

Coverage matrix (tests target the canonical paths the dispatcher relies on):

  * connect() / disconnect() — establishes + tears down the IB session
  * connection_state() — reads cached state without I/O
  * place_order() — three branches: limit_marketable, stop_market, market
  * place_order rejection handling: status='rejected' surfaces with
    rejection_category derived from IBKR error code map
  * place_order error: raises IbkrPlacementError on terminal failure
  * cancel_order() — happy path + KeyError on missing client_order_id
  * cancel_all_orders() — batch cancel
  * get_positions() — empty + populated
  * get_account_summary() — fetches NetLiquidation + margin metrics
  * resolve_contract() — futures + ETF + KeyError on out-of-universe
  * Decimal precision preserved through float coercion (A05)
  * Timestamps tz-aware UTC (A06)

A22 N/A (no audit_log writes in PR-B scope). A06 enforced. A27 covered
by ``deploy/ibkr/README.md`` operator runbook. A02 BINDS — these tests
verify the contract; the implementation requires `risk-review-approved`.

Strategy: tests inject a fake `IB` class via the adapter's ``ib_factory``
parameter. The fake IB class implements just enough of the ib_async
surface to make the adapter's calls compile + return controllable values.
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.ibkr_adapter import IbAsyncIbkrClient
from services.execution.types import (
    IbkrContractRef,
    IbkrPlacementError,
    IbkrPlaceOrderRequest,
)


def _fake_ib_class(**method_returns: Any) -> type:
    """Construct a fake ib_async.IB class with the given method behaviors.

    The fake exposes the surface the adapter calls into: connectAsync,
    disconnect, isConnected, placeOrder, cancelOrder, openTrades,
    positions, accountSummaryAsync, qualifyContractsAsync, serverVersion.
    """

    class _FakeIB:
        def __init__(self) -> None:
            self.serverVersion = method_returns.get("server_version", 176)
            self._connected = False
            self.connectAsync = AsyncMock(side_effect=self._mark_connected)
            self.qualifyContractsAsync = AsyncMock()
            self.accountSummaryAsync = AsyncMock(
                return_value=method_returns.get("account_summary", [])
            )
            self.placeOrder = MagicMock(
                return_value=method_returns.get("place_order_trade", _default_trade())
            )
            self.cancelOrder = MagicMock()
            self.openTrades = MagicMock(return_value=method_returns.get("open_trades", []))
            self.positions = MagicMock(return_value=method_returns.get("positions", []))

        async def _mark_connected(self, **_kw: Any) -> None:
            if method_returns.get("connect_raises"):
                raise method_returns["connect_raises"]
            self._connected = True

        def isConnected(self) -> bool:
            return self._connected

        def disconnect(self) -> None:
            self._connected = False

    return _FakeIB


def _default_trade(
    *,
    order_id: int = 12345,
    status: str = "Submitted",
    rejection_error: tuple[int, str] | None = None,
) -> MagicMock:
    """Build a fake ib_async Trade object."""
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.orderStatus.status = status
    if rejection_error is not None:
        log_entry = MagicMock()
        log_entry.errorCode = rejection_error[0]
        log_entry.message = rejection_error[1]
        trade.log = [log_entry]
    else:
        trade.log = []
    return trade


def _basic_contract() -> IbkrContractRef:
    return IbkrContractRef(
        market="/MES",
        ibkr_local_symbol="MESH26",
        ibkr_con_id=12345,
        multiplier=5,
        exchange="CME",
    )


def _basic_request() -> IbkrPlaceOrderRequest:
    return IbkrPlaceOrderRequest(
        client_order_id="9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1",
        contract=_basic_contract(),
        side="buy",
        quantity=Decimal("1"),
        order_type="limit_marketable",
        limit_price=Decimal("4000"),
        time_in_force="DAY",
    )


# ---------------------------------------------------------------------------
# TestConnect
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_succeeds(self) -> None:
        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        state = await client.connect()
        assert state.is_connected is True
        assert state.server_version == 176

    async def test_connect_idempotent(self) -> None:
        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()
        # Second connect should noop (no exception).
        state = await client.connect()
        assert state.is_connected is True

    async def test_connect_raises_placement_error_on_failure(self) -> None:
        fake = _fake_ib_class(connect_raises=ConnectionRefusedError("port closed"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        with pytest.raises(IbkrPlacementError) as exc_info:
            await client.connect()
        # IbkrPlacementError is a frozen dataclass + an Exception subclass;
        # the raise machinery sets .args on Exception but the dataclass
        # field name is `operation`. Both should reflect the call site.
        err = exc_info.value
        assert err.operation == "connect"
        assert err.underlying_exception_class == "ConnectionRefusedError"


# ---------------------------------------------------------------------------
# TestConnectionState
# ---------------------------------------------------------------------------


class TestConnectionState:
    async def test_state_before_connect(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        state = await client.connection_state()
        assert state.is_connected is False
        assert "not called yet" in (state.last_error or "")

    async def test_state_after_connect(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        await client.connect()
        state = await client.connection_state()
        assert state.is_connected is True
        assert state.server_version == 176


# ---------------------------------------------------------------------------
# TestPlaceOrder
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    async def test_limit_marketable_submitted(self) -> None:
        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        result = await client.place_order(_basic_request())
        assert result.status == "submitted"
        assert result.broker_order_id == 12345
        assert result.client_order_id == "9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1"
        assert result.rejection_category is None

    async def test_market_order(self) -> None:
        fake = _fake_ib_class(place_order_trade=_default_trade(status="Filled"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        req = _basic_request()
        # Rebuild as market order
        from dataclasses import replace

        req = replace(req, order_type="market", limit_price=None)
        result = await client.place_order(req)
        assert result.status == "filled"

    async def test_limit_marketable_requires_limit_price(self) -> None:
        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        from dataclasses import replace

        req = replace(_basic_request(), limit_price=None)
        with pytest.raises(IbkrPlacementError) as exc_info:
            await client.place_order(req)
        # The ValueError raised inside place_order gets wrapped into
        # IbkrPlacementError with underlying_exception_class="ValueError".
        assert exc_info.value.underlying_exception_class == "ValueError"

    async def test_rejection_with_known_error_code(self) -> None:
        fake = _fake_ib_class(
            place_order_trade=_default_trade(
                status="Inactive",
                rejection_error=(201, "Order rejected - Reason: Margin"),
            )
        )
        client = IbAsyncIbkrClient(ib_factory=fake)
        result = await client.place_order(_basic_request())
        assert result.status == "rejected"
        assert result.rejection_category == "insufficient_margin"
        assert "Margin" in (result.rejection_detail or "")

    async def test_rejection_with_unknown_error_code(self) -> None:
        fake = _fake_ib_class(
            place_order_trade=_default_trade(
                status="Inactive",
                rejection_error=(99999, "Some never-seen error"),
            )
        )
        client = IbAsyncIbkrClient(ib_factory=fake)
        result = await client.place_order(_basic_request())
        assert result.status == "rejected"
        assert result.rejection_category == "unknown"

    async def test_placement_error_on_network_failure(self) -> None:
        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        # Replace placeOrder to raise after connect
        await client.connect()
        client._ib.placeOrder.side_effect = TimeoutError("TWS API timeout")  # type: ignore[union-attr]
        with pytest.raises(IbkrPlacementError) as exc_info:
            await client.place_order(_basic_request())
        assert exc_info.value.operation == "placeOrder"
        assert exc_info.value.underlying_exception_class == "TimeoutError"


# ---------------------------------------------------------------------------
# TestCancelOrder
# ---------------------------------------------------------------------------


class TestCancelOrder:
    async def test_cancel_existing(self) -> None:
        trade = MagicMock()
        trade.order.orderId = 12345
        trade.order.orderRef = "9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1"
        fake = _fake_ib_class(open_trades=[trade])
        client = IbAsyncIbkrClient(ib_factory=fake)
        result = await client.cancel_order("9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1")
        assert result.broker_order_id == 12345
        assert result.client_order_id == "9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1"
        # cancelOrder was called with the order
        client._ib.cancelOrder.assert_called_once_with(trade.order)  # type: ignore[union-attr]

    async def test_cancel_missing_raises_key_error(self) -> None:
        fake = _fake_ib_class(open_trades=[])
        client = IbAsyncIbkrClient(ib_factory=fake)
        with pytest.raises(KeyError, match="not found"):
            await client.cancel_order("nonexistent-id")


# ---------------------------------------------------------------------------
# TestCancelAllOrders
# ---------------------------------------------------------------------------


class TestCancelAllOrders:
    async def test_cancel_all_with_trades(self) -> None:
        trade_a = MagicMock()
        trade_a.order.orderId = 1
        trade_b = MagicMock()
        trade_b.order.orderId = 2
        fake = _fake_ib_class(open_trades=[trade_a, trade_b])
        client = IbAsyncIbkrClient(ib_factory=fake)
        count = await client.cancel_all_orders()
        assert count == 2
        assert client._ib.cancelOrder.call_count == 2  # type: ignore[union-attr]

    async def test_cancel_all_empty(self) -> None:
        fake = _fake_ib_class(open_trades=[])
        client = IbAsyncIbkrClient(ib_factory=fake)
        count = await client.cancel_all_orders()
        assert count == 0


# ---------------------------------------------------------------------------
# TestGetPositions
# ---------------------------------------------------------------------------


class TestGetPositions:
    async def test_empty_positions(self) -> None:
        fake = _fake_ib_class(positions=[])
        client = IbAsyncIbkrClient(ib_factory=fake)
        result = await client.get_positions()
        assert result == []

    async def test_populated_positions_with_decimal_precision(self) -> None:
        ib_pos = MagicMock()
        ib_pos.position = 1.5  # float — must be coerced to Decimal('1.5')
        ib_pos.avgCost = 4234.567
        ib_pos.contract.symbol = "MES"
        ib_pos.contract.localSymbol = "MESH26"
        ib_pos.contract.conId = 12345
        ib_pos.contract.secType = "FUT"
        ib_pos.contract.exchange = "CME"
        ib_pos.contract.multiplier = 5
        fake = _fake_ib_class(positions=[ib_pos])
        client = IbAsyncIbkrClient(ib_factory=fake)
        result = await client.get_positions()
        assert len(result) == 1
        # Decimal precision preserved through str() round-trip per A05.
        assert result[0].quantity == Decimal("1.5")
        assert result[0].avg_cost_usd == Decimal("4234.567")
        assert result[0].contract.market == "/MES"


# ---------------------------------------------------------------------------
# TestGetAccountSummary
# ---------------------------------------------------------------------------


class TestGetAccountSummary:
    async def test_account_summary(self) -> None:
        rows = [
            MagicMock(tag="NetLiquidation", value="15000.00"),
            MagicMock(tag="AvailableFunds", value="12000.00"),
            MagicMock(tag="InitMarginReq", value="3000.00"),
            MagicMock(tag="MaintMarginReq", value="2400.00"),
            MagicMock(tag="BuyingPower", value="48000.00"),
        ]
        fake = _fake_ib_class(account_summary=rows)
        client = IbAsyncIbkrClient(ib_factory=fake, account_id="U25655583")
        summary = await client.get_account_summary()
        assert summary.account_id == "U25655583"
        assert summary.net_liquidation_usd == Decimal("15000.00")
        assert summary.available_funds_usd == Decimal("12000.00")
        assert summary.init_margin_req_usd == Decimal("3000.00")
        # Tz-aware UTC per A06.
        assert summary.snapshot_at_utc.tzinfo == UTC


# ---------------------------------------------------------------------------
# TestResolveContract
# ---------------------------------------------------------------------------


class TestResolveContract:
    async def test_resolve_futures_market(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ref = await client.resolve_contract("/MES")
        assert ref.market == "/MES"
        assert ref.exchange == "CME"
        assert ref.multiplier == 5

    async def test_resolve_etf_market(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ref = await client.resolve_contract("TLT")
        assert ref.market == "TLT"
        assert ref.exchange == "SMART"
        assert ref.multiplier == 1

    async def test_resolve_out_of_universe_raises_key_error(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        with pytest.raises(KeyError, match="not in Phase 1 universe"):
            await client.resolve_contract("/BTC")

    async def test_resolve_cached(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ref1 = await client.resolve_contract("/MES")
        ref2 = await client.resolve_contract("/MES")
        # Same object instance (cache hit).
        assert ref1 is ref2


# ---------------------------------------------------------------------------
# TestModuleContract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_public_surface_exported(self) -> None:
        from services import execution

        for name in (
            "IbAsyncIbkrClient",
            "IbkrClient",
            "IbkrPlaceOrderRequest",
            "IbkrPlaceOrderResult",
            "IbkrPlacementError",
            "IbkrContractRef",
            "IbkrPosition",
            "IbkrAccountSummary",
            "DEFAULT_CLIENT_ID",
        ):
            assert hasattr(execution, name), f"missing public export: {name}"

    def test_default_client_id_is_one(self) -> None:
        from services.execution import DEFAULT_CLIENT_ID

        assert DEFAULT_CLIENT_ID == 1
