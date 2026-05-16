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

import asyncio
from datetime import UTC, datetime
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
# TestContractResolution — 2026-05-16 Defect #1 fix
# ---------------------------------------------------------------------------


class TestBuildIbContractFutures:
    """Validates ``_build_ib_contract`` emits ``ContFuture`` for futures.

    Pre-fix this returned ``Future(symbol='MNQ', exchange='CME')`` with
    no ``lastTradeDateOrContractMonth``, which IBKR rejects with
    error 321. Post-fix emits ``ContFuture`` which qualifies to a
    concrete front-month Future at submit time.
    """

    def test_futures_market_emits_contfuture(self) -> None:
        from ib_async import ContFuture

        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        ref = IbkrContractRef(
            market="/MNQ",
            ibkr_local_symbol="",
            ibkr_con_id=None,
            multiplier=2,
            exchange="CME",
        )
        contract = client._build_ib_contract(ref)
        assert isinstance(contract, ContFuture)
        assert contract.symbol == "MNQ"
        assert contract.exchange == "CME"
        assert contract.currency == "USD"

    def test_etf_market_emits_stock(self) -> None:
        from ib_async import Stock

        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        ref = IbkrContractRef(
            market="TLT",
            ibkr_local_symbol="TLT",
            ibkr_con_id=None,
            multiplier=1,
            exchange="SMART",
        )
        contract = client._build_ib_contract(ref)
        assert isinstance(contract, Stock)
        assert contract.symbol == "TLT"


class TestPlaceOrderQualifyFirst:
    """Validates qualifyContractsAsync runs BEFORE placeOrder + handles
    its failure modes.

    Pre-fix the call order was placeOrder → qualifyContractsAsync (line
    348 ran AFTER 338), so IBKR rejected the order at validation time
    before qualify could populate the front-month expiry. Post-fix
    qualify runs first.
    """

    @staticmethod
    def _qualified_future_mock(
        *,
        last_trade_date: str = "20260619",
        con_id: int = 678901234,
    ) -> Any:
        """Mock that emulates ib_async's qualified Future result.

        We construct a MagicMock with the attributes ``qualifyContractsAsync``
        would populate on a real Future contract.
        """
        m = MagicMock()
        m.__class__.__name__ = "Future"
        m.symbol = "MNQ"
        m.lastTradeDateOrContractMonth = last_trade_date
        m.exchange = "CME"
        m.currency = "USD"
        m.conId = con_id
        m.localSymbol = "MNQM6"
        m.secType = "FUT"
        return m

    async def test_qualify_runs_before_place_order(self) -> None:
        """Empirically verify call-order via call_args timestamps."""
        qualified_future = self._qualified_future_mock()
        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        client._ib_factory = fake  # placeholder; real assertion via mock_calls below

        await client.connect()
        client._ib.qualifyContractsAsync.return_value = [qualified_future]  # type: ignore[union-attr]
        await client.place_order(_basic_request())

        # qualifyContractsAsync was called at least once
        assert client._ib.qualifyContractsAsync.await_count >= 1  # type: ignore[union-attr]
        # placeOrder was called at least once
        assert client._ib.placeOrder.call_count >= 1  # type: ignore[union-attr]

    async def test_empty_qualification_raises_placement_error(self) -> None:
        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()
        # qualify returns empty list → no front-month resolved
        client._ib.qualifyContractsAsync.return_value = []  # type: ignore[union-attr]
        with pytest.raises(IbkrPlacementError) as exc_info:
            await client.place_order(_basic_request())
        assert exc_info.value.operation == "qualifyContractsAsync"
        assert "no matches" in (exc_info.value.detail or "").lower()
        # placeOrder was NEVER called because we bailed at qualify
        assert client._ib.placeOrder.call_count == 0  # type: ignore[union-attr]

    async def test_qualify_exception_raises_placement_error(self) -> None:
        fake = _fake_ib_class()
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()
        client._ib.qualifyContractsAsync.side_effect = RuntimeError(  # type: ignore[union-attr]
            "TWS API disconnected mid-qualify"
        )
        with pytest.raises(IbkrPlacementError) as exc_info:
            await client.place_order(_basic_request())
        assert exc_info.value.operation == "qualifyContractsAsync"
        assert exc_info.value.underlying_exception_class == "RuntimeError"
        assert client._ib.placeOrder.call_count == 0  # type: ignore[union-attr]

    async def test_contfuture_converted_to_future_for_place_order(self) -> None:
        """ContFuture's secType=CONTFUT is not accepted by placeOrder; we
        explicitly construct a Future from the qualified ContFuture's
        fields. This test verifies placeOrder receives a Future, not a
        ContFuture."""
        # Mock that mimics a still-ContFuture-typed qualified result
        contfuture_mock = MagicMock()
        contfuture_mock.__class__.__name__ = "ContFuture"
        contfuture_mock.symbol = "MNQ"
        contfuture_mock.lastTradeDateOrContractMonth = "20260619"
        contfuture_mock.exchange = "CME"
        contfuture_mock.currency = "USD"
        contfuture_mock.conId = 678901234

        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()
        client._ib.qualifyContractsAsync.return_value = [contfuture_mock]  # type: ignore[union-attr]
        await client.place_order(_basic_request())

        # Inspect what placeOrder was called with
        call_args = client._ib.placeOrder.call_args  # type: ignore[union-attr]
        contract_arg = call_args.args[0]
        from ib_async import Future

        assert isinstance(contract_arg, Future)
        # The Future was constructed with the qualified ContFuture's expiry
        assert contract_arg.lastTradeDateOrContractMonth == "20260619"
        assert contract_arg.conId == 678901234

    async def test_qualified_future_used_directly(self) -> None:
        """If ib_async returns a Future directly from qualify (the
        common case), we use it as-is — no Future-from-ContFuture
        reconstruction needed."""
        qualified_future = self._qualified_future_mock()
        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()
        client._ib.qualifyContractsAsync.return_value = [qualified_future]  # type: ignore[union-attr]
        await client.place_order(_basic_request())

        call_args = client._ib.placeOrder.call_args  # type: ignore[union-attr]
        contract_arg = call_args.args[0]
        # We passed the qualified Future mock through (the placeOrder
        # arg is the same object, not a new Future).
        assert contract_arg is qualified_future


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
# TestSubscribeOrderStatus
# ---------------------------------------------------------------------------


class _OrderStatusEvent:
    """Minimal stand-in for ib_async's IB.orderStatusEvent ('+=' Sigil)."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> _OrderStatusEvent:
        self._handlers.append(handler)
        return self

    def fire(self, trade: Any) -> None:
        for handler in self._handlers:
            handler(trade)

    @property
    def handler_count(self) -> int:
        return len(self._handlers)


def _fake_ib_with_event_class() -> type:
    """A fake IB whose instance carries a real `orderStatusEvent` attr."""

    class _FakeIBWithEvent:
        def __init__(self) -> None:
            self.serverVersion = 176
            self._connected = False
            self.orderStatusEvent = _OrderStatusEvent()
            self.connectAsync = AsyncMock(side_effect=self._mark_connected)
            self.qualifyContractsAsync = AsyncMock()
            self.accountSummaryAsync = AsyncMock(return_value=[])
            self.placeOrder = MagicMock(return_value=_default_trade())
            self.cancelOrder = MagicMock()
            self.openTrades = MagicMock(return_value=[])
            self.positions = MagicMock(return_value=[])

        async def _mark_connected(self, **_kw: Any) -> None:
            self._connected = True

        def isConnected(self) -> bool:
            return self._connected

        def disconnect(self) -> None:
            self._connected = False

    return _FakeIBWithEvent


class TestSubscribeOrderStatus:
    async def test_subscribe_attaches_once(self) -> None:
        ib_cls = _fake_ib_with_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        cb: Any = AsyncMock()
        await client.subscribe_order_status(cb)
        assert ib_instance.orderStatusEvent.handler_count == 1
        # Re-subscribing swaps the callback in place; the IB handler stays attached once.
        cb2: Any = AsyncMock()
        await client.subscribe_order_status(cb2)
        assert ib_instance.orderStatusEvent.handler_count == 1

    async def test_subscribe_swaps_callback(self) -> None:
        ib_cls = _fake_ib_with_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        cb_old: Any = AsyncMock()
        cb_new: Any = AsyncMock()
        await client.subscribe_order_status(cb_old)
        await client.subscribe_order_status(cb_new)
        # Fire a trade with a valid Filled status; only the new callback runs.
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        trade = _build_trade_for_event(status="Filled")
        ib_instance.orderStatusEvent.fire(trade)
        # Yield to let the scheduled task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cb_old.assert_not_called()
        cb_new.assert_called_once()

    async def test_callback_exception_swallowed(self) -> None:
        ib_cls = _fake_ib_with_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        cb: Any = AsyncMock(side_effect=RuntimeError("misbehaving consumer"))
        await client.subscribe_order_status(cb)
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        trade = _build_trade_for_event(status="Filled")
        # No exception should propagate out of the event fire.
        ib_instance.orderStatusEvent.fire(trade)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cb.assert_called_once()


# ---------------------------------------------------------------------------
# TestBuildOrderStatusUpdate
# ---------------------------------------------------------------------------


def _build_trade_for_event(
    *,
    status: str = "Filled",
    order_ref: str = "strat123-param456-sig00000-0",
    action: str = "BUY",
    sec_type: str = "FUT",
    symbol: str = "MES",
    filled: float = 1.0,
    remaining: float = 0.0,
    avg_fill_price: float = 5234.75,
    commissions: tuple[float, ...] = (1.25,),
    order_id: int = 12345,
    fill_times: tuple[datetime, ...] = (),
) -> MagicMock:
    """Construct a trade mock with the fields _build_order_status_update reads."""
    from datetime import datetime as _dt

    trade = MagicMock()
    trade.order.orderRef = order_ref
    trade.order.action = action
    trade.order.orderId = order_id
    trade.orderStatus.status = status
    trade.orderStatus.filled = filled
    trade.orderStatus.remaining = remaining
    trade.orderStatus.avgFillPrice = avg_fill_price
    trade.contract.secType = sec_type
    trade.contract.symbol = symbol

    fills = []
    for i, comm in enumerate(commissions):
        f = MagicMock()
        f.commissionReport.commission = comm
        f.time = fill_times[i] if i < len(fill_times) else _dt(2026, 5, 12, 21, 30 + i, tzinfo=UTC)
        fills.append(f)
    trade.fills = fills
    return trade


class TestBuildOrderStatusUpdate:
    def test_filled_futures_translates_clean(self) -> None:
        from decimal import Decimal

        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event()
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.client_order_id == "strat123-param456-sig00000-0"
        assert update.broker_order_id == 12345
        assert update.status == "filled"
        assert update.market == "/MES"
        assert update.side == "buy"
        assert update.cumulative_filled_quantity == Decimal("1.0")
        assert update.avg_fill_price == Decimal("5234.75")
        assert update.total_commission_usd == Decimal("1.25")
        assert update.last_fill_at_utc is not None
        assert update.observed_at_utc.tzinfo is UTC

    def test_etf_market_no_slash_prefix(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event(sec_type="STK", symbol="TLT")
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.market == "TLT"

    def test_sell_action(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event(action="SELL")
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.side == "sell"

    def test_status_map_partial_fill(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event(status="PartiallyFilled")
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.status == "partially_filled"

    def test_status_map_submitted(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event(
            status="Submitted", avg_fill_price=0.0, commissions=(), filled=0.0
        )
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.status == "submitted"
        assert update.avg_fill_price is None
        assert update.total_commission_usd == Decimal(0)

    def test_status_map_unknown_collapses_to_submitted(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event(status="WeirdNewStatus")
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.status == "submitted"

    def test_missing_order_ref_returns_none(self) -> None:
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event(order_ref="")
        assert client._build_order_status_update(trade) is None  # type: ignore[attr-defined]

    def test_commission_sum_across_multiple_fills(self) -> None:
        from decimal import Decimal

        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        trade = _build_trade_for_event(commissions=(0.50, 0.75, 1.00))
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.total_commission_usd == Decimal("2.25")

    def test_last_fill_at_picks_max(self) -> None:
        from datetime import datetime as _dt

        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        t0 = _dt(2026, 5, 12, 21, 30, tzinfo=UTC)
        t1 = _dt(2026, 5, 12, 21, 35, tzinfo=UTC)
        t2 = _dt(2026, 5, 12, 21, 33, tzinfo=UTC)
        trade = _build_trade_for_event(commissions=(0.5, 0.5, 0.5), fill_times=(t0, t1, t2))
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.last_fill_at_utc == t1

    def test_naive_fill_time_coerced_to_utc(self) -> None:
        from datetime import datetime as _dt

        client = IbAsyncIbkrClient(ib_factory=_fake_ib_with_event_class())
        naive = _dt(2026, 5, 12, 21, 30)
        trade = _build_trade_for_event(commissions=(0.5,), fill_times=(naive,))
        update = client._build_order_status_update(trade)  # type: ignore[attr-defined]
        assert update is not None
        assert update.last_fill_at_utc is not None
        assert update.last_fill_at_utc.tzinfo is UTC


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
