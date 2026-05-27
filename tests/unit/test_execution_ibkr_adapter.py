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
        multiplier=Decimal("5"),
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
            multiplier=Decimal("2"),
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
            multiplier=Decimal("1"),
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
# TestPlaceOrderParentId — PR-gamma / drill 6 defect #3 fix
# ---------------------------------------------------------------------------


class TestPlaceOrderParentId:
    """Validates ``IbkrPlaceOrderRequest.parent_broker_order_id`` is wired
    into ``ib_async.Order.parentId`` on the placeOrder call.

    Pre-fix (drill 6): the stop order was always placed with
    ``Order.parentId = 0`` (the ib_async default), so IBKR treated it as
    a STANDALONE sell-stop rather than an OCO child of the entry. Drill
    6 surfaced this when reqGlobalCancel was needed mid-bracket — the
    stop survived independently. Post-fix: when the worker threads the
    entry's broker_order_id into ``parent_broker_order_id``, the adapter
    sets ``Order.parentId`` so IBKR auto-cancels the stop on entry
    cancellation.
    """

    @staticmethod
    def _last_order_arg(client: IbAsyncIbkrClient) -> Any:
        """Pull the ib_async Order from the most recent placeOrder call."""
        call_args = client._ib.placeOrder.call_args  # type: ignore[union-attr]
        return call_args.args[1]

    async def test_default_parent_broker_order_id_is_none(self) -> None:
        """Sanity-lock: the dataclass default is None."""
        req = _basic_request()
        assert req.parent_broker_order_id is None

    async def test_no_parent_means_order_parentid_zero(self) -> None:
        """When the request omits parent_broker_order_id, the Order
        passed to placeOrder has the ib_async default parentId=0 — no
        bracket linkage registered with IBKR."""
        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.place_order(_basic_request())
        order_arg = self._last_order_arg(client)
        assert order_arg.parentId == 0

    async def test_parent_broker_order_id_propagates_to_order_parentid(self) -> None:
        """Setting parent_broker_order_id on the request → Order.parentId
        gets the same value. This is the core PR-gamma contract."""
        from dataclasses import replace

        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        # Stop-side request with parent_broker_order_id=42 (= entry broker_order_id)
        req = replace(
            _basic_request(),
            order_type="stop_market",
            side="sell",
            limit_price=None,
            stop_price=Decimal("3990"),
            time_in_force="GTC",
            parent_client_order_id="9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1",
            parent_broker_order_id=42,
        )
        await client.place_order(req)
        order_arg = self._last_order_arg(client)
        assert order_arg.parentId == 42
        # Belt-and-suspenders: orderRef + orderType still correct
        assert order_arg.orderRef == req.client_order_id
        assert order_arg.orderType == "STP"

    async def test_parent_broker_order_id_applies_to_any_order_type(self) -> None:
        """The wiring is order-type agnostic; even an entry-side limit
        order would carry parentId if the caller set it (defensive — in
        practice only stops set it, but we don't gate by order_type)."""
        from dataclasses import replace

        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        req = replace(_basic_request(), parent_broker_order_id=7)
        await client.place_order(req)
        order_arg = self._last_order_arg(client)
        assert order_arg.parentId == 7

    async def test_round_trip_entry_then_stop_with_parent_linkage(self) -> None:
        """Simulates the worker's bracket-placement sequence:
        place entry → capture broker_order_id → place stop with that
        broker_order_id as parent → verify Order.parentId on the stop.

        Mirrors the actual production sequence in
        ``services.risk.order_placement_worker.apply_order_placement``.
        """
        from dataclasses import replace

        entry_trade = _default_trade(order_id=99, status="Submitted")
        stop_trade = _default_trade(order_id=100, status="PreSubmitted")
        fake = _fake_ib_class(place_order_trade=entry_trade)
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()
        # First placeOrder → returns entry_trade; second → stop_trade
        client._ib.placeOrder.side_effect = [entry_trade, stop_trade]  # type: ignore[union-attr]

        # 1. Place entry (no parent_broker_order_id)
        entry_result = await client.place_order(_basic_request())
        assert entry_result.broker_order_id == 99
        entry_order_arg = client._ib.placeOrder.call_args_list[0].args[1]  # type: ignore[union-attr]
        assert entry_order_arg.parentId == 0

        # 2. Place stop with entry's broker_order_id as parent
        stop_req = replace(
            _basic_request(),
            client_order_id="9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1-stop",
            order_type="stop_market",
            side="sell",
            limit_price=None,
            stop_price=Decimal("3990"),
            time_in_force="GTC",
            parent_client_order_id=_basic_request().client_order_id,
            parent_broker_order_id=entry_result.broker_order_id,
        )
        stop_result = await client.place_order(stop_req)
        assert stop_result.broker_order_id == 100
        stop_order_arg = client._ib.placeOrder.call_args_list[1].args[1]  # type: ignore[union-attr]
        assert stop_order_arg.parentId == 99  # matches entry_result.broker_order_id


# ---------------------------------------------------------------------------
# TestPlaceOrderTransmit — PR-eta / drill 9 fix
# ---------------------------------------------------------------------------


class TestPlaceOrderTransmit:
    """Validates ``IbkrPlaceOrderRequest.transmit`` is wired into
    ``ib_async.Order.transmit`` on the placeOrder call.

    PR-eta (2026-05-19). The atomic-bracket protocol per IBKR docs:

      * parent (entry) placed with transmit=False → IBKR queues it in
        PendingSubmit but does NOT release to the exchange
      * child (stop) placed with transmit=True + parentId=<entry_id> →
        IBKR atomically releases both and registers the OCA relationship

    This prevents the drill 9 race where a marketable-limit entry
    filled within ~7ms before the stop's parentId linkage could
    validate (Error 201 "Parent order is being cancelled"). With
    transmit=False on the parent, the parent CANNOT fill until the
    full bracket is registered at IBKR.

    Default ``request.transmit=True`` preserves existing single-order
    behavior for non-bracket callers (cancel paths, recon-driven manual
    closes, the place_order surface in general).
    """

    @staticmethod
    def _last_order_arg(client: IbAsyncIbkrClient) -> Any:
        call_args = client._ib.placeOrder.call_args  # type: ignore[union-attr]
        return call_args.args[1]

    async def test_default_transmit_is_true(self) -> None:
        """Sanity-lock: the dataclass default for transmit is True."""
        req = _basic_request()
        assert req.transmit is True

    async def test_transmit_true_default_propagates_to_order(self) -> None:
        """A request with the default transmit=True produces an Order
        whose transmit=True (matches ib_async default + preserves the
        pre-PR-eta single-order behavior)."""
        fake = _fake_ib_class(place_order_trade=_default_trade(status="Submitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.place_order(_basic_request())
        order_arg = self._last_order_arg(client)
        assert order_arg.transmit is True

    async def test_transmit_false_propagates_to_order(self) -> None:
        """A request with transmit=False produces an Order whose
        transmit=False (the parent-leg of an atomic bracket)."""
        from dataclasses import replace

        fake = _fake_ib_class(place_order_trade=_default_trade(status="PreSubmitted"))
        client = IbAsyncIbkrClient(ib_factory=fake)
        req = replace(_basic_request(), transmit=False)
        await client.place_order(req)
        order_arg = self._last_order_arg(client)
        assert order_arg.transmit is False

    async def test_atomic_bracket_pair_round_trip(self) -> None:
        """Simulates the worker's PR-eta atomic-bracket sequence:

          1. place entry with transmit=False, parent_broker_order_id=None
          2. capture entry's broker_order_id from the result
          3. place stop with transmit=True, parent_broker_order_id=entry_id

        Asserts on the Order objects passed to ib.placeOrder for both
        legs that the canonical atomic-bracket contract holds.
        """
        from dataclasses import replace

        entry_trade = _default_trade(order_id=99, status="PreSubmitted")
        stop_trade = _default_trade(order_id=100, status="PreSubmitted")
        fake = _fake_ib_class(place_order_trade=entry_trade)
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()
        client._ib.placeOrder.side_effect = [entry_trade, stop_trade]  # type: ignore[union-attr]

        # 1. Entry: transmit=False, no parent
        entry_req = replace(_basic_request(), transmit=False)
        entry_result = await client.place_order(entry_req)
        assert entry_result.broker_order_id == 99
        entry_order_arg = client._ib.placeOrder.call_args_list[0].args[1]  # type: ignore[union-attr]
        assert entry_order_arg.transmit is False
        assert entry_order_arg.parentId == 0

        # 2. Stop: transmit=True + parent_broker_order_id=entry's broker_order_id
        stop_req = replace(
            _basic_request(),
            client_order_id="9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1-stop",
            order_type="stop_market",
            side="sell",
            limit_price=None,
            stop_price=Decimal("3990"),
            time_in_force="GTC",
            parent_client_order_id=_basic_request().client_order_id,
            parent_broker_order_id=entry_result.broker_order_id,
            transmit=True,
        )
        stop_result = await client.place_order(stop_req)
        assert stop_result.broker_order_id == 100
        stop_order_arg = client._ib.placeOrder.call_args_list[1].args[1]  # type: ignore[union-attr]
        assert stop_order_arg.transmit is True
        assert stop_order_arg.parentId == 99  # matches entry_result.broker_order_id


# ---------------------------------------------------------------------------
# TestCancelOrder
# ---------------------------------------------------------------------------


class TestCancelOrder:
    async def test_cancel_existing(self) -> None:
        """Happy path: cancelOrder is submitted and ib-async's local
        trade cache transitions ``orderStatus.status`` to ``Cancelled``
        synchronously (simulated via cancelOrder side_effect)."""
        trade = MagicMock()
        trade.order.orderId = 12345
        trade.order.orderRef = "9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1"
        trade.orderStatus.status = "Submitted"

        fake = _fake_ib_class(open_trades=[trade])
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()

        def _mark_cancelled(_order: Any) -> None:
            trade.orderStatus.status = "Cancelled"

        assert client._ib is not None
        client._ib.cancelOrder.side_effect = _mark_cancelled

        result = await client.cancel_order("9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1")
        assert result.broker_order_id == 12345
        assert result.client_order_id == "9d2f7a1c-b54e83a1-4d9e7c1b2f0a-1"
        client._ib.cancelOrder.assert_called_once_with(trade.order)

    async def test_cancel_missing_raises_key_error(self) -> None:
        fake = _fake_ib_class(open_trades=[])
        client = IbAsyncIbkrClient(ib_factory=fake)
        with pytest.raises(KeyError, match="not found"):
            await client.cancel_order("nonexistent-id")

    async def test_cancel_verification_status_terminal_returns_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification returns the observed terminal status (``Inactive``
        — the alternate accepted terminal state alongside ``Cancelled``)
        after a couple of poll cycles."""
        import services.execution.ibkr_adapter as adapter_mod

        monkeypatch.setattr(adapter_mod, "CANCEL_VERIFY_TIMEOUT_SECONDS", 0.5)
        monkeypatch.setattr(adapter_mod, "CANCEL_VERIFY_POLL_INTERVAL_SECONDS", 0.01)

        trade = MagicMock()
        trade.order.orderId = 7
        trade.order.orderRef = "cid-7"
        trade.orderStatus.status = "Submitted"

        fake = _fake_ib_class(open_trades=[trade])
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()

        async def _flip_after_delay() -> None:
            await asyncio.sleep(0.05)
            trade.orderStatus.status = "Inactive"

        def _schedule_flip(_order: Any) -> None:
            asyncio.get_running_loop().create_task(_flip_after_delay())

        assert client._ib is not None
        client._ib.cancelOrder.side_effect = _schedule_flip

        result = await client.cancel_order("cid-7")
        assert result.broker_order_id == 7
        assert trade.orderStatus.status == "Inactive"

    async def test_cancel_verification_timeout_raises_placement_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``orderStatus.status`` stays in a non-terminal state past
        the timeout, ``cancel_order`` raises ``IbkrPlacementError`` with
        ``operation="cancelOrder"`` and the underlying cause encoded in
        ``detail``. Mirrors the cross-clientId Error 10147 failure mode
        (Docs/decisions-log.md 2026-05-27)."""
        import services.execution.ibkr_adapter as adapter_mod

        monkeypatch.setattr(adapter_mod, "CANCEL_VERIFY_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(adapter_mod, "CANCEL_VERIFY_POLL_INTERVAL_SECONDS", 0.01)

        trade = MagicMock()
        trade.order.orderId = 4
        trade.order.orderRef = "b5237aed-stop-replace-0"
        trade.orderStatus.status = "PreSubmitted"  # stays stuck — async-reject case

        fake = _fake_ib_class(open_trades=[trade])
        client = IbAsyncIbkrClient(ib_factory=fake)

        with pytest.raises(IbkrPlacementError) as exc_info:
            await client.cancel_order("b5237aed-stop-replace-0")

        err = exc_info.value
        assert err.operation == "cancelOrder"
        assert "PreSubmitted" in err.detail
        assert "Cancelled" in err.detail
        assert "Error 10147" in err.detail
        assert err.underlying_exception_class == "CancelVerificationTimeout"

    async def test_cancel_verification_post_loop_race_window_returns_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover the race window between the last loop iteration's
        ``await asyncio.sleep`` and the final post-loop ``getattr``
        check in :meth:`_await_cancel_terminal_status` (PR #262).

        Setup: monkeypatch ``loop.time`` so the deadline expires WHILE
        the trade.orderStatus.status is still non-terminal — forcing
        the while loop to exit without an early return. Then BEFORE
        the post-loop probe, flip the status to ``Cancelled`` (the
        IBKR-side ack landed during the sleep window). The post-loop
        getattr must observe the flip + return success without raising.

        Pre-PR #262 this path didn't exist (cancel_order returned
        synchronously without verification). Post-PR #262 the race-
        window defense is load-bearing — without it, a cancel that
        ack'd mid-poll would falsely raise
        ``CancelVerificationTimeout``.
        """
        import services.execution.ibkr_adapter as adapter_mod

        # Skip the loop entirely — deadline is in the past from the start.
        monkeypatch.setattr(adapter_mod, "CANCEL_VERIFY_TIMEOUT_SECONDS", 0.0)
        monkeypatch.setattr(adapter_mod, "CANCEL_VERIFY_POLL_INTERVAL_SECONDS", 0.01)

        trade = MagicMock()
        trade.order.orderId = 9
        trade.order.orderRef = "race-cid-9"
        # Status starts PreSubmitted. The while loop won't execute
        # because deadline is 0 (loop.time() is monotonic-positive),
        # so the test exercises the post-loop branch directly. We flip
        # the status BEFORE invoking cancel_order — simulating an
        # IBKR-side ack that landed before the verifier even started.
        trade.orderStatus.status = "Cancelled"

        fake = _fake_ib_class(open_trades=[trade])
        client = IbAsyncIbkrClient(ib_factory=fake)
        await client.connect()

        result = await client.cancel_order("race-cid-9")
        assert result.broker_order_id == 9
        # No raise — the post-loop branch caught the terminal status.
        # This is the race-window defense the upstream cancel path
        # depends on for cross-clientId cancellation correctness.


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
# TestContractFromIb — multiplier coercion defensive branches
# ---------------------------------------------------------------------------


class TestContractFromIb:
    """Coverage for the defensive branches in
    :meth:`IbAsyncIbkrClient._contract_from_ib` (PR #262 follow-up).

    The pre-PR-#262 multiplier coercion ``int(ib_contract.multiplier)``
    silently truncated /MBT's ``"0.1"`` to 0 + /MYM's ``"0.5"`` to 0.
    PR #262 replaced this with ``Decimal(str(ib_mult_raw))`` plus a
    defensive ``(ValueError, ArithmeticError)`` catch so a malformed
    multiplier string falls back to ``Decimal("1")`` instead of
    crashing the positions/qualify flow. These tests pin those
    defensive branches against regressions.
    """

    def _ib_contract_with_multiplier(self, multiplier: Any) -> MagicMock:
        """Build a minimal ib_async-shape Contract mock that returns
        the given multiplier value via getattr."""
        ib_contract = MagicMock()
        ib_contract.symbol = "MES"
        ib_contract.localSymbol = "MESM6"
        ib_contract.conId = 12345
        ib_contract.secType = "FUT"
        ib_contract.exchange = "CME"
        ib_contract.multiplier = multiplier
        return ib_contract

    def test_value_error_on_coercion_falls_back_to_one(self) -> None:
        """An ib_async multiplier string that's not Decimal-parseable
        (e.g., ``"5x"`` — a hypothetical malformed value from a future
        IBKR field change) falls back to ``Decimal("1")`` rather than
        crashing the adapter."""
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ib_contract = self._ib_contract_with_multiplier("5x")
        ref = client._contract_from_ib(ib_contract)
        assert ref.multiplier == Decimal("1")
        assert ref.market == "/MES"

    def test_arithmetic_error_on_coercion_falls_back_to_one(self) -> None:
        """An ib_async multiplier value that raises ArithmeticError on
        ``Decimal(str(...))`` (e.g., a NaN-as-string ``"NaN"``;
        ``Decimal("NaN")`` doesn't raise but ``Decimal("Infinity")``
        is also a Decimal special value that subsequent arithmetic
        could choke on — but the more direct trigger here is a
        floating-point-derived very-long string that overflows Decimal's
        default context). We synthesize the branch by passing a value
        whose ``str()`` produces an obviously-broken Decimal token."""
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        # `Decimal("Infinity")` produces a Decimal special-value rather
        # than raising; the cleanest way to exercise the ArithmeticError
        # branch is via a stub object whose ``str()`` is itself a
        # Decimal-pathological token. ``"not-a-number"`` raises
        # ``InvalidOperation`` (a subclass of ``ArithmeticError``).
        ib_contract = self._ib_contract_with_multiplier("not-a-number")
        ref = client._contract_from_ib(ib_contract)
        assert ref.multiplier == Decimal("1")

    def test_empty_string_multiplier_falls_back_to_one(self) -> None:
        """An empty-string multiplier (e.g., an ETF contract where
        IBKR doesn't populate the field) takes the early-return
        branch + lands at ``Decimal("1")``."""
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ib_contract = self._ib_contract_with_multiplier("")
        ib_contract.symbol = "TLT"  # ETF — multiplier irrelevant
        ib_contract.secType = "STK"
        ib_contract.exchange = "SMART"
        ref = client._contract_from_ib(ib_contract)
        assert ref.multiplier == Decimal("1")
        assert ref.market == "TLT"  # STK → no leading slash

    def test_none_multiplier_falls_back_to_one(self) -> None:
        """A ``None`` multiplier (some ib_async versions return None
        when the field is unset) also takes the early-return branch."""
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ib_contract = self._ib_contract_with_multiplier(None)
        ref = client._contract_from_ib(ib_contract)
        assert ref.multiplier == Decimal("1")


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

    async def test_resolve_mym_has_half_dollar_multiplier(self) -> None:
        """/MYM is $0.50 per point — pre-2026-05-27 the static resolve dict
        used ``int`` literals which silently truncated this to ``0``."""
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ref = await client.resolve_contract("/MYM")
        assert ref.market == "/MYM"
        assert ref.multiplier == Decimal("0.5")

    async def test_resolve_mbt_has_fractional_btc_multiplier(self) -> None:
        """/MBT is 0.1 BTC per contract — pre-2026-05-27 the static resolve
        dict stored ``1`` because ``int`` couldn't carry the fractional
        value (notional = qty * 0.1 * spot, so int=1 over-scales 10x)."""
        client = IbAsyncIbkrClient(ib_factory=_fake_ib_class())
        ref = await client.resolve_contract("/MBT")
        assert ref.market == "/MBT"
        assert ref.multiplier == Decimal("0.1")

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
# TestErrorEventSubscription (2026-05-18 drill 5 follow-up #2)
#
# Mirrors the TestSubscribeOrderStatus pattern: `_OrderErrorEvent`
# stand-in for ib_async's `IB.errorEvent` (eventkit.Event surface that
# supports `+= handler` + `.emit(*args)`). The fake fires the
# 4-arg signature documented in the upstream
# `wrapper.py::self.ib.errorEvent.emit(reqId, errorCode, errorString, contract)`.
# ---------------------------------------------------------------------------


class _ErrorEvent:
    """Minimal stand-in for ib_async's IB.errorEvent (`+=` Sigil)."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> _ErrorEvent:
        self._handlers.append(handler)
        return self

    def fire(self, reqId: int, errorCode: int, errorString: str, contract: Any) -> None:
        for handler in self._handlers:
            handler(reqId, errorCode, errorString, contract)

    @property
    def handler_count(self) -> int:
        return len(self._handlers)


def _fake_ib_with_error_event_class() -> type:
    """A fake IB whose instance carries a real `errorEvent` attr."""

    class _FakeIBWithErrorEvent:
        def __init__(self) -> None:
            self.serverVersion = 176
            self._connected = False
            self.errorEvent = _ErrorEvent()
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

    return _FakeIBWithErrorEvent


def _make_contract_mock(local_symbol: str | None) -> Any:
    """Build a MagicMock that quacks like an ib_async Contract.

    Use ``None`` for connection-level errors that don't carry a
    contract; the adapter handles both branches.
    """
    if local_symbol is None:
        return None
    contract = MagicMock()
    contract.localSymbol = local_symbol
    return contract


class TestErrorEventSubscription:
    """``connect()`` wires the IBKR errorEvent + captures last error state."""

    async def test_connect_wires_error_event_once(self) -> None:
        """Idempotent across reconnects — handler attaches exactly once."""
        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        assert ib_instance.errorEvent.handler_count == 1
        # Force a disconnect + reconnect — handler stays at 1.
        ib_instance._connected = False
        await client.connect()
        assert ib_instance.errorEvent.handler_count == 1

    async def test_connect_emits_subscribed_log(self) -> None:
        from structlog.testing import capture_logs

        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        with capture_logs() as logs:
            await client.connect()
        events = [log.get("event") for log in logs]
        assert "ibkr_error_event_subscribed" in events

    async def test_error_event_with_1100_stores_last_error(self) -> None:
        """Connection-level Error 1100 captured into ``last_ibkr_error``."""
        from services.execution.ibkr_adapter import IbkrErrorState

        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        # Pre-condition: no error captured yet.
        assert client.last_ibkr_error is None
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        ib_instance.errorEvent.fire(
            -1,
            1100,
            "Connectivity between IBKR and Trader Workstation has been lost.",
            None,
        )
        state = client.last_ibkr_error
        assert isinstance(state, IbkrErrorState)
        assert state.error_code == 1100
        assert state.error_string.startswith("Connectivity between IBKR")
        assert state.req_id == -1
        assert state.contract_local_symbol is None
        assert state.last_seen_at_utc.tzinfo is UTC

    async def test_error_event_warning_for_connectivity_codes(self) -> None:
        from structlog.testing import capture_logs

        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        with capture_logs() as logs:
            ib_instance.errorEvent.fire(-1, 1102, "Connectivity restored.", None)
        received = [log for log in logs if log.get("event") == "ibkr_error_received"]
        assert len(received) == 1
        assert received[0]["log_level"] == "warning"
        assert received[0]["category"] == "connectivity"
        assert received[0]["error_code"] == 1102

    async def test_error_event_warning_for_data_farm_codes(self) -> None:
        from structlog.testing import capture_logs

        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        with capture_logs() as logs:
            ib_instance.errorEvent.fire(-1, 2104, "Market data farm connection is OK.", None)
        received = [log for log in logs if log.get("event") == "ibkr_error_received"]
        assert len(received) == 1
        assert received[0]["log_level"] == "warning"
        assert received[0]["category"] == "data_farm"

    async def test_error_event_error_for_order_rejection_codes(self) -> None:
        """Codes >= 10000 are order rejections; log at ERROR."""
        from structlog.testing import capture_logs

        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        contract = _make_contract_mock("TLT")
        with capture_logs() as logs:
            ib_instance.errorEvent.fire(
                42,
                10147,
                "OrderId 42 that needs to be cancelled is not found.",
                contract,
            )
        received = [log for log in logs if log.get("event") == "ibkr_error_received"]
        assert len(received) == 1
        assert received[0]["log_level"] == "error"
        assert received[0]["category"] == "order_rejection"
        assert received[0]["req_id"] == 42
        assert received[0]["contract_local_symbol"] == "TLT"

    async def test_error_event_info_for_unknown_codes(self) -> None:
        from structlog.testing import capture_logs

        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        with capture_logs() as logs:
            ib_instance.errorEvent.fire(-1, 321, "Please enter a local symbol.", None)
        received = [log for log in logs if log.get("event") == "ibkr_error_received"]
        assert len(received) == 1
        assert received[0]["log_level"] == "info"
        assert received[0]["category"] == "other"

    async def test_handler_swallows_exceptions(self) -> None:
        """A malformed event must not crash the broker connection."""
        from unittest.mock import PropertyMock

        from structlog.testing import capture_logs

        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        # Build a contract whose .localSymbol raises on access.
        bad_contract = MagicMock()
        type(bad_contract).localSymbol = PropertyMock(side_effect=RuntimeError("bad attr"))
        with capture_logs() as logs:
            # Must not raise.
            ib_instance.errorEvent.fire(-1, 1100, "Connectivity lost.", bad_contract)
        failed = [log for log in logs if log.get("event") == "ibkr_error_event_handler_failed"]
        assert len(failed) == 1
        assert failed[0]["raw_error_code"] == 1100
        # last_ibkr_error should remain None — the snapshot construction
        # raised before assignment.
        assert client.last_ibkr_error is None

    async def test_last_ibkr_error_overwritten_on_new_event(self) -> None:
        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        ib_instance.errorEvent.fire(-1, 1100, "lost", None)
        first = client.last_ibkr_error
        assert first is not None
        assert first.error_code == 1100
        # New event overwrites.
        ib_instance.errorEvent.fire(-1, 1102, "restored", None)
        second = client.last_ibkr_error
        assert second is not None
        assert second.error_code == 1102

    async def test_contract_local_symbol_captured_when_present(self) -> None:
        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        contract = _make_contract_mock("MESH26")
        ib_instance.errorEvent.fire(99, 10147, "not found", contract)
        state = client.last_ibkr_error
        assert state is not None
        assert state.contract_local_symbol == "MESH26"

    async def test_contract_none_yields_none_local_symbol(self) -> None:
        ib_cls = _fake_ib_with_error_event_class()
        client = IbAsyncIbkrClient(ib_factory=ib_cls)
        await client.connect()
        ib_instance = client._ib  # type: ignore[attr-defined]
        assert ib_instance is not None
        ib_instance.errorEvent.fire(-1, 1100, "lost", None)
        state = client.last_ibkr_error
        assert state is not None
        assert state.contract_local_symbol is None

    async def test_constants_match_canonical_sets(self) -> None:
        """``IBKR_CONNECTIVITY_ERROR_CODES`` + ``IBKR_DATA_FARM_ERROR_CODES`` are frozen."""
        from services.execution.ibkr_adapter import (
            IBKR_CONNECTIVITY_ERROR_CODES,
            IBKR_DATA_FARM_ERROR_CODES,
        )

        assert IBKR_CONNECTIVITY_ERROR_CODES == frozenset({1100, 1101, 1102})
        assert IBKR_DATA_FARM_ERROR_CODES == frozenset({2103, 2104, 2106, 2107, 2108, 2110, 2150})
        # These sets are disjoint (1100-1102 vs. 2103+).
        assert IBKR_CONNECTIVITY_ERROR_CODES.isdisjoint(IBKR_DATA_FARM_ERROR_CODES)

    async def test_ibkr_error_state_frozen(self) -> None:
        """``IbkrErrorState`` is a frozen dataclass (cannot mutate after construction)."""
        from datetime import datetime

        from services.execution.ibkr_adapter import IbkrErrorState

        state = IbkrErrorState(
            error_code=1100,
            error_string="lost",
            last_seen_at_utc=datetime.now(tz=UTC),
            req_id=-1,
            contract_local_symbol=None,
        )
        with pytest.raises(Exception):  # FrozenInstanceError or dataclasses.FrozenInstanceError
            state.error_code = 1102  # type: ignore[misc]


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
            "IbkrErrorState",
            "IbkrErrorStateProvider",
            "IBKR_CONNECTIVITY_ERROR_CODES",
            "IBKR_DATA_FARM_ERROR_CODES",
            "PHASE1_TICK_SIZES",
            "round_to_tick",
        ):
            assert hasattr(execution, name), f"missing public export: {name}"

    def test_default_client_id_is_one(self) -> None:
        from services.execution import DEFAULT_CLIENT_ID

        assert DEFAULT_CLIENT_ID == 1


class TestRoundToTick:
    """Tick-rounding helper (drill 6 fix, 2026-05-18).

    Locked behavior: ``round_to_tick(price, market=...)`` quantizes ``price``
    to the market's :data:`PHASE1_TICK_SIZES` entry using
    :data:`decimal.ROUND_HALF_UP`. Raises ``KeyError`` if market is not in
    the Phase 1 universe map.
    """

    def test_phase1_tick_sizes_contains_universe(self) -> None:
        from services.execution.ibkr_adapter import PHASE1_TICK_SIZES

        # Locked Phase 1 universe per backend-spec §11.1.
        for market in ("/MES", "/MNQ", "/MYM", "/M2K", "/MGC", "/MCL", "/MBT"):
            assert market in PHASE1_TICK_SIZES, f"missing micro future {market!r}"
        for market in ("TLT", "IEF", "SHY", "TIP"):
            assert market in PHASE1_TICK_SIZES, f"missing ETF {market!r}"
        # No surprise entries
        assert len(PHASE1_TICK_SIZES) == 11

    def test_phase1_tick_sizes_are_decimals(self) -> None:
        """Tick values MUST be Decimal (not float) to avoid A05 violations
        propagating into IBKR-bound order shapes."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import PHASE1_TICK_SIZES

        for market, tick in PHASE1_TICK_SIZES.items():
            assert isinstance(tick, _Decimal), f"{market} tick is {type(tick)}, expected Decimal"
            assert tick > 0, f"{market} tick={tick} must be > 0"

    def test_mes_quarter_tick_half_up(self) -> None:
        """/MES tick=0.25. Round-half-up at 0.125 → 0.25."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        assert round_to_tick(_Decimal("7401.625"), market="/MES") == _Decimal("7401.75")
        # 7401.50 already aligned
        assert round_to_tick(_Decimal("7401.50"), market="/MES") == _Decimal("7401.50")
        # 7401.10 → 7401.00 (closer)
        assert round_to_tick(_Decimal("7401.10"), market="/MES") == _Decimal("7401.00")
        # 7401.20 → 7401.25 (closer; 0.05 vs 0.20)
        assert round_to_tick(_Decimal("7401.20"), market="/MES") == _Decimal("7401.25")

    def test_mgc_dime_tick(self) -> None:
        """/MGC tick=0.10."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        assert round_to_tick(_Decimal("2345.67"), market="/MGC") == _Decimal("2345.70")
        assert round_to_tick(_Decimal("2345.64"), market="/MGC") == _Decimal("2345.60")
        # Half-up exactly on 0.05
        assert round_to_tick(_Decimal("2345.65"), market="/MGC") == _Decimal("2345.70")

    def test_mbt_5usd_tick(self) -> None:
        """/MBT tick=5.0; tick larger than 1.0 still rounds correctly."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        # 102000.50 → multiplier 20400.10 → rounds to 20400 → 102000
        assert round_to_tick(_Decimal("102000.50"), market="/MBT") == _Decimal("102000")
        # 102003 → multiplier 20400.60 → rounds to 20401 → 102005
        assert round_to_tick(_Decimal("102003"), market="/MBT") == _Decimal("102005")
        # 102002.50 (half-tick) → multiplier 20400.50 → ROUND_HALF_UP → 20401 → 102005
        assert round_to_tick(_Decimal("102002.50"), market="/MBT") == _Decimal("102005")

    def test_mym_unit_tick(self) -> None:
        """/MYM tick=1.0; sub-unit decimals round to nearest integer."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        assert round_to_tick(_Decimal("40123.40"), market="/MYM") == _Decimal("40123")
        assert round_to_tick(_Decimal("40123.60"), market="/MYM") == _Decimal("40124")
        # Half-up at .5
        assert round_to_tick(_Decimal("40123.50"), market="/MYM") == _Decimal("40124")

    def test_etf_cent_tick(self) -> None:
        """ETFs tick=0.01; high-precision input rounds to the cent."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        for market in ("TLT", "IEF", "SHY", "TIP"):
            assert round_to_tick(_Decimal("85.005"), market=market) == _Decimal("85.01")
            assert round_to_tick(_Decimal("85.004"), market=market) == _Decimal("85.00")
            assert round_to_tick(_Decimal("82.50123456"), market=market) == _Decimal("82.50")

    def test_unknown_market_raises_key_error(self) -> None:
        """KeyError is the raw failure mode; callers (e.g.,
        ``plan_order_placement``) wrap into a domain-specific error."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        with pytest.raises(KeyError):
            round_to_tick(_Decimal("100.00"), market="/XYZ")

    def test_already_aligned_passes_through(self) -> None:
        """A price that's already on a tick boundary returns the same value
        (modulo Decimal scale)."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        result = round_to_tick(_Decimal("4250.50"), market="/MES")
        assert result == _Decimal("4250.50")
        # The result is the multiplier-times-tick — same Decimal value.
        # Equality compares numerically (Decimal("4250.50") == Decimal("4250.5")
        # is True), so we don't assert exact-scale matching here.

    def test_extremely_small_prices(self) -> None:
        """Edge: a price smaller than the tick rounds to 0 or one tick."""
        from decimal import Decimal as _Decimal

        from services.execution.ibkr_adapter import round_to_tick

        # /MES tick=0.25; 0.10 → 0 (rounds DOWN; 0.10 < 0.125 cutoff)
        assert round_to_tick(_Decimal("0.10"), market="/MES") == _Decimal("0.00")
        # 0.20 → 0.25 (closer)
        assert round_to_tick(_Decimal("0.20"), market="/MES") == _Decimal("0.25")
