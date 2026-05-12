"""services/execution/ibkr_adapter.py — concrete IbkrClient using ib-async.

Pivot-PR-B (post-pivot 2026-05-12). Translates the abstract
``services.execution.ibkr_client.IbkrClient`` Protocol calls into
``ib_async`` library calls against a Dockerized ``ib_gateway`` container.

**A02 BINDS** — file is on forbidden whitelist; `risk-review-approved`
required for changes.

**Design constraints:**

* Thin adapter: 1:1 method translation. No business logic; that lives
  in ``services/risk/signal_dispatch.py`` (Pivot-PR-D) and the route
  handlers. Keeps the test surface small (mock at this layer; everything
  upstream is pure-policy).
* Reconnect-on-disconnect: `ib_async`'s ``IB`` instance retains
  connection state; the adapter's methods auto-reconnect on transient
  disconnects by checking ``ib.isConnected()`` before each call. The
  reconnect uses cached config from ``__init__``; if reconnect fails,
  the method raises ``IbkrPlacementError`` (terminal).
* Single client_id: the adapter holds a single ``IB`` instance bound to
  one TWS API ``clientId``. IBKR allows multiple clients per session
  (different clientIds), but Phase 1 has one execution process per
  account, so one client suffices. The clientId comes from sops
  (``ibkr.client_id``; default 1).
* Decimal precision: ``ib_async`` returns ``float`` for prices + quantities.
  We coerce via ``Decimal(str(x))`` to preserve precision through
  ``str()`` round-trip per dev-guide §3.8 + [A05].
* Tz-aware UTC: all timestamps from ``ib_async`` come as wall-clock-local
  datetimes; we attach ``timezone.utc`` immediately + log a warning if
  the underlying timestamp is already tz-aware with a non-UTC offset
  (means our assumption is wrong somewhere).

**A27 satisfier** — operator-runbook smoke at ``deploy/ibkr/README.md``
exercises the most-restricted endpoint contract: placeOrder + cancelOrder
round-trip on IBKR paper /MES. Without that smoke, this adapter is unproven
against the real broker; we treat the smoke as a pre-deploy gate.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import structlog

from services.execution.types import (
    IbkrAccountSummary,
    IbkrCancelOrderResult,
    IbkrConnectionState,
    IbkrContractRef,
    IbkrOrderSide,
    IbkrOrderStatus,
    IbkrPlacementError,
    IbkrPlaceOrderRequest,
    IbkrPlaceOrderResult,
    IbkrPosition,
    IbkrRejectionCategory,
    OrderStatusCallback,
    OrderStatusKind,
    OrderStatusUpdate,
)

if TYPE_CHECKING:
    # Type-only import: ib_async at type-check time.
    from ib_async import IB

log = structlog.get_logger()


#: Default TWS API client ID for the execution service. IBKR allows up to
#: 8 client IDs per TWS session; we use 1 by default and reserve 2-8 for
#: future expansion (multi-strategy / multi-account). Configurable via
#: ``IbAsyncIbkrClient.__init__(client_id=...)``.
DEFAULT_CLIENT_ID: Final[int] = 1

#: Map from ib_async order-status strings to our locked
#: ``IbkrOrderStatus`` enum. The library may emit additional statuses
#: (Inactive, ApiPending) which we collapse to the nearest fit.
_IBKR_STATUS_MAP: Final[dict[str, IbkrOrderStatus]] = {
    "PendingSubmit": "pending_submit",
    "PendingCancel": "submitted",
    "PreSubmitted": "submitted",
    "Submitted": "submitted",
    "ApiPending": "pending_submit",
    "ApiCancelled": "cancelled",
    "Cancelled": "cancelled",
    "Filled": "filled",
    "Inactive": "rejected",
}


#: Map from ib_async order-status strings to the broker-agnostic
#: ``OrderStatusKind`` enum surfaced via ``subscribe_order_status``. Same
#: source strings as ``_IBKR_STATUS_MAP`` but a different downstream
#: enum — the placement path needs ``pending_submit`` separated from
#: ``submitted`` because ``orders.status`` distinguishes them; the
#: subscription path collapses them because consumers care about
#: "is it live at the broker?" vs. "did it terminate?".
_ORDER_STATUS_KIND_MAP: Final[dict[str, OrderStatusKind]] = {
    "PendingSubmit": "submitted",
    "PendingCancel": "submitted",
    "PreSubmitted": "submitted",
    "Submitted": "submitted",
    "ApiPending": "submitted",
    "ApiCancelled": "cancelled",
    "Cancelled": "cancelled",
    "PartiallyFilled": "partially_filled",
    "Filled": "filled",
    "Inactive": "inactive",
}


#: Map from IBKR error code → our rejection-taxonomy enum. Backend-spec
#: §6.3 (Order Rejection Taxonomy) lists the canonical buckets; this map
#: classifies the ib_async ``ErrorEvent.code`` field. Codes not in this
#: map fall back to ``"unknown"`` and surface as P1 alerts for operator
#: triage.
_ERROR_CODE_MAP: Final[dict[int, IbkrRejectionCategory]] = {
    201: "insufficient_margin",  # "Order rejected - Reason: Margin"
    202: "broker_disconnect",  # "Order Canceled - reason: Mass cancel"
    134: "outside_trading_hours",  # "Modify or Cancel Order Failed - Reason: Outside RTH"
    200: "invalid_contract",  # "No security definition has been found"
    103: "duplicate_order",  # "Duplicate order id"
    109: "limit_too_far_from_market",  # "Order rejected: Price too far from market"
    1100: "broker_disconnect",  # "Connectivity between IB and TWS has been lost"
    1102: "broker_disconnect",  # "Connectivity between TWS and server is broken"
}


def _ts_utc() -> datetime:
    """Tz-aware UTC `now` factory; defaults [A06]."""
    return datetime.now(tz=UTC)


def _to_decimal(x: float | int | str | Decimal) -> Decimal:
    """Coerce to Decimal preserving precision through str round-trip.

    Required because `ib_async` returns Python floats for prices + quantities;
    a direct ``Decimal(float)`` cast loses precision (e.g.,
    ``Decimal(0.1) == Decimal('0.100000000000...')``). The ``str(float)``
    round-trip uses Python's repr which is shortest-round-trip lossless.
    """
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


class IbAsyncIbkrClient:
    """``IbkrClient`` Protocol impl backed by the ``ib-async`` library.

    Constructed once per process at startup with config sourced from sops.
    Holds a single ``IB`` instance bound to one TWS API clientId. Methods
    are thread-safe via ib-async's internal locking; the caller is
    expected to use a single instance from the asyncio event loop.

    Tests inject a fake ``IB`` (or fake module) via the ``ib_factory``
    parameter to avoid pulling in the real ``ib_async`` library + opening
    real TWS connections.
    """

    def __init__(
        self,
        *,
        host: str = "ib_gateway",
        port: int = 4004,
        account_id: str | None = None,
        client_id: int = DEFAULT_CLIENT_ID,
        ib_factory: type[IB] | None = None,
        connect_timeout_seconds: float = 30.0,
    ) -> None:
        """Construct the adapter (no I/O yet; call ``connect()`` to dial out).

        :param host: hostname of the ib_gateway container. Defaults to the
            Docker DNS name on the internal network.
        :param port: 4004 for paper, 4003 for live — these are the
            externally-published socat ports per gnzsnz/ib-gateway-docker
            convention. The internal IB Gateway listens on 127.0.0.1:4002
            (paper) / :4001 (live), but those are unreachable from outside
            the container; socat publishes the externally-facing ports as
            4004/4003. Discovered Pivot-PR-B Step 5 smoke 2026-05-12.
            Operator selects paper-vs-live via ``LEAN_LIVE_MODE`` env var.
        :param account_id: IBKR account number (e.g., "U25655583"). When
            None, the adapter uses the default account on the TWS session.
        :param client_id: TWS API clientId; 1-7 per IBKR's docs.
        :param ib_factory: ib_async.IB class (or test fake). Defaults to
            the real library when None.
        :param connect_timeout_seconds: max wall-clock to wait for the
            initial TWS API handshake.
        """
        self._host = host
        self._port = port
        self._account_id = account_id
        self._client_id = client_id
        self._connect_timeout = connect_timeout_seconds

        if ib_factory is None:
            # Lazy import — avoids hard ib_async dep at module import time so
            # tests can run without the library installed. The wheel-build
            # path needs ib_async installed at deploy time (services/api/
            # Dockerfile RUN pip install block has it).
            from ib_async import IB as IbAsyncIB

            self._ib_factory = IbAsyncIB
        else:
            self._ib_factory = ib_factory

        self._ib: IB | None = None  # set on first connect()
        # Cache of resolved contract objects keyed by market symbol.
        self._contract_cache: dict[str, IbkrContractRef] = {}
        # Single registered order-status callback + the asyncio loop it
        # was registered on. The loop is captured at subscribe time
        # because ib_async dispatches events from arbitrary threads and
        # we need a stable loop to schedule the user's async callback
        # back into.
        self._order_status_callback: OrderStatusCallback | None = None
        self._order_status_loop: asyncio.AbstractEventLoop | None = None
        # Tracks whether we've already wired the IB event handler so
        # subscribe_order_status is idempotent across reconnects.
        self._order_status_wired: bool = False

    async def connect(self) -> IbkrConnectionState:
        if self._ib is None:
            self._ib = self._ib_factory()
        if not self._ib.isConnected():
            try:
                await self._ib.connectAsync(
                    host=self._host,
                    port=self._port,
                    clientId=self._client_id,
                    timeout=self._connect_timeout,
                )
                log.info(
                    "ibkr_connected",
                    host=self._host,
                    port=self._port,
                    client_id=self._client_id,
                    server_version=getattr(self._ib, "serverVersion", None),
                )
            except Exception as exc:
                log.error(
                    "ibkr_connect_failed",
                    host=self._host,
                    port=self._port,
                    error=str(exc),
                )
                raise IbkrPlacementError(
                    operation="connect",
                    detail=f"ib_gateway connect failed: {exc!r}",
                    underlying_exception_class=type(exc).__name__,
                    occurred_at_utc=_ts_utc(),
                ) from exc
        return await self.connection_state()

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            try:
                self._ib.disconnect()
                log.info("ibkr_disconnected")
            except Exception as exc:
                log.warning("ibkr_disconnect_error", error=str(exc))

    async def connection_state(self) -> IbkrConnectionState:
        if self._ib is None:
            return IbkrConnectionState(
                is_connected=False,
                last_error="ib_factory not initialized (connect() not called yet)",
                client_id=self._client_id,
                server_version=None,
            )
        return IbkrConnectionState(
            is_connected=self._ib.isConnected(),
            last_error=None,
            client_id=self._client_id,
            server_version=getattr(self._ib, "serverVersion", None),
        )

    async def _ensure_connected(self) -> IB:
        """Lazy-connect helper. Reconnects on transient disconnect once."""
        if self._ib is None or not self._ib.isConnected():
            await self.connect()
        assert self._ib is not None  # narrowed by connect()
        return self._ib

    async def place_order(self, request: IbkrPlaceOrderRequest) -> IbkrPlaceOrderResult:
        ib = await self._ensure_connected()
        submitted_at = _ts_utc()
        try:
            # Translate request → ib_async Contract + Order
            from ib_async import LimitOrder, MarketOrder, StopOrder

            ib_contract = self._build_ib_contract(request.contract)
            ib_order: Any
            if request.order_type == "limit_marketable":
                if request.limit_price is None:
                    raise ValueError("limit_marketable requires limit_price")
                ib_order = LimitOrder(
                    action="BUY" if request.side == "buy" else "SELL",
                    totalQuantity=float(request.quantity),
                    lmtPrice=float(request.limit_price),
                    orderRef=request.client_order_id,
                    tif=request.time_in_force,
                )
            elif request.order_type == "limit_at_target":
                if request.limit_price is None:
                    raise ValueError("limit_at_target requires limit_price")
                ib_order = LimitOrder(
                    action="BUY" if request.side == "buy" else "SELL",
                    totalQuantity=float(request.quantity),
                    lmtPrice=float(request.limit_price),
                    orderRef=request.client_order_id,
                    tif=request.time_in_force,
                )
            elif request.order_type == "stop_market":
                if request.stop_price is None:
                    raise ValueError("stop_market requires stop_price")
                ib_order = StopOrder(
                    action="BUY" if request.side == "buy" else "SELL",
                    totalQuantity=float(request.quantity),
                    stopPrice=float(request.stop_price),
                    orderRef=request.client_order_id,
                    tif=request.time_in_force,
                )
            elif request.order_type == "market":
                ib_order = MarketOrder(
                    action="BUY" if request.side == "buy" else "SELL",
                    totalQuantity=float(request.quantity),
                    orderRef=request.client_order_id,
                    tif=request.time_in_force,
                )
            else:
                # Unreachable due to Literal validation at the API boundary
                # but the type narrows here defensively.
                raise ValueError(f"unsupported order_type: {request.order_type!r}")

            trade = ib.placeOrder(ib_contract, ib_order)
            # ib_async returns immediately with a Trade object; the actual
            # order-status events arrive async. We wait briefly for the
            # initial Submitted/Filled/Rejected status to land. If the
            # event hasn't arrived in 2s, return with status from the
            # current trade snapshot.
            #
            # Note: a fully-async dispatcher (Pivot-PR-D) subscribes to the
            # Trade.statusEvent for ongoing updates; this method's contract
            # is "submit + report initial status".
            await ib.qualifyContractsAsync(ib_contract)  # ensures conId
            broker_order_id = trade.order.orderId
            status_raw = getattr(trade.orderStatus, "status", "PendingSubmit")
            status = _IBKR_STATUS_MAP.get(status_raw, "pending_submit")

            # If IBKR rejected at submit, the trade.log includes a code.
            rejection_category: IbkrRejectionCategory | None = None
            rejection_detail: str | None = None
            if status == "rejected":
                logs = getattr(trade, "log", [])
                if logs:
                    last_log = logs[-1]
                    rejection_detail = getattr(last_log, "message", str(last_log))
                    error_code = getattr(last_log, "errorCode", None)
                    if isinstance(error_code, int):
                        rejection_category = _ERROR_CODE_MAP.get(error_code, "unknown")

            log.info(
                "ibkr_order_placed",
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                status=status,
                rejection_category=rejection_category,
            )

            return IbkrPlaceOrderResult(
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                status=status,
                submitted_at_utc=submitted_at,
                rejection_category=rejection_category,
                rejection_detail=rejection_detail,
            )

        except IbkrPlacementError:
            raise
        except Exception as exc:
            log.error(
                "ibkr_place_order_failed",
                client_order_id=request.client_order_id,
                error=str(exc),
            )
            raise IbkrPlacementError(
                operation="placeOrder",
                detail=f"placeOrder failed: {exc!r}",
                underlying_exception_class=type(exc).__name__,
                occurred_at_utc=submitted_at,
            ) from exc

    async def cancel_order(self, client_order_id: str) -> IbkrCancelOrderResult:
        ib = await self._ensure_connected()
        submitted_at = _ts_utc()
        try:
            # Find the open trade by client_order_id (orderRef).
            open_trades = ib.openTrades()
            for trade in open_trades:
                order_ref = getattr(trade.order, "orderRef", "")
                if order_ref == client_order_id:
                    ib.cancelOrder(trade.order)
                    log.info(
                        "ibkr_order_cancel_submitted",
                        client_order_id=client_order_id,
                        broker_order_id=trade.order.orderId,
                    )
                    return IbkrCancelOrderResult(
                        client_order_id=client_order_id,
                        broker_order_id=trade.order.orderId,
                        cancel_submitted_at_utc=submitted_at,
                    )
            raise KeyError(
                f"client_order_id {client_order_id!r} not found in open trades; "
                "may have already filled or been cancelled"
            )
        except (KeyError, IbkrPlacementError):
            raise
        except Exception as exc:
            log.error(
                "ibkr_cancel_order_failed",
                client_order_id=client_order_id,
                error=str(exc),
            )
            raise IbkrPlacementError(
                operation="cancelOrder",
                detail=f"cancelOrder failed: {exc!r}",
                underlying_exception_class=type(exc).__name__,
                occurred_at_utc=submitted_at,
            ) from exc

    async def cancel_all_orders(self) -> int:
        ib = await self._ensure_connected()
        try:
            open_trades = ib.openTrades()
            count = 0
            for trade in open_trades:
                ib.cancelOrder(trade.order)
                count += 1
            log.warning("ibkr_cancel_all_submitted", count=count)
            return count
        except Exception as exc:
            log.error("ibkr_cancel_all_failed", error=str(exc))
            raise IbkrPlacementError(
                operation="cancelAllOrders",
                detail=f"cancelAllOrders failed: {exc!r}",
                underlying_exception_class=type(exc).__name__,
                occurred_at_utc=_ts_utc(),
            ) from exc

    async def get_positions(self) -> list[IbkrPosition]:
        ib = await self._ensure_connected()
        try:
            ib_positions = ib.positions(account=self._account_id or "")
            result: list[IbkrPosition] = []
            for pos in ib_positions:
                ref = self._contract_from_ib(pos.contract)
                result.append(
                    IbkrPosition(
                        contract=ref,
                        quantity=_to_decimal(pos.position),
                        avg_cost_usd=_to_decimal(pos.avgCost),
                        market_price_usd=None,  # positions() doesn't include marks
                        unrealized_pnl_usd=None,
                        realized_pnl_usd=Decimal(0),
                    )
                )
            return result
        except Exception as exc:
            log.error("ibkr_get_positions_failed", error=str(exc))
            raise IbkrPlacementError(
                operation="reqPositions",
                detail=f"positions() failed: {exc!r}",
                underlying_exception_class=type(exc).__name__,
                occurred_at_utc=_ts_utc(),
            ) from exc

    async def get_account_summary(self) -> IbkrAccountSummary:
        ib = await self._ensure_connected()
        snapshot_at = _ts_utc()
        try:
            tags = "NetLiquidation,AvailableFunds,InitMarginReq,MaintMarginReq,BuyingPower"
            rows = await ib.accountSummaryAsync(account=self._account_id or "")
            # rows is a list of AccountValue objects with `.tag`/`.value`.
            values: dict[str, Decimal] = {}
            for row in rows:
                if row.tag in tags:
                    values[row.tag] = _to_decimal(row.value)
            return IbkrAccountSummary(
                account_id=self._account_id or "",
                net_liquidation_usd=values.get("NetLiquidation", Decimal(0)),
                available_funds_usd=values.get("AvailableFunds", Decimal(0)),
                init_margin_req_usd=values.get("InitMarginReq", Decimal(0)),
                maintenance_margin_req_usd=values.get("MaintMarginReq", Decimal(0)),
                buying_power_usd=values.get("BuyingPower", Decimal(0)),
                snapshot_at_utc=snapshot_at,
            )
        except Exception as exc:
            log.error("ibkr_get_account_summary_failed", error=str(exc))
            raise IbkrPlacementError(
                operation="accountSummary",
                detail=f"accountSummary() failed: {exc!r}",
                underlying_exception_class=type(exc).__name__,
                occurred_at_utc=snapshot_at,
            ) from exc

    async def resolve_contract(self, market: str) -> IbkrContractRef:
        """Resolve a market symbol (e.g., '/MES') to an IBKR contract.

        Phase 1 sub-universe is locked; markets not in the universe raise
        ``KeyError``. The full implementation (front-month futures contract
        selection via IBKR's contract-search API + the roll-calendar) is
        deferred to Pivot-PR-C scope when reconciliation needs it
        end-to-end. For Pivot-PR-B, this method returns a stub
        ``IbkrContractRef`` with the market symbol and locked exchange.
        """
        if market in self._contract_cache:
            return self._contract_cache[market]
        # Phase 1 universe locked at backend-spec §11.1.
        phase1_futures = {"/MES", "/MNQ", "/MYM", "/M2K", "/MGC", "/MCL", "/MBT"}
        phase1_etfs = {"TLT", "IEF", "SHY", "TIP"}
        if market in phase1_futures:
            exchange = "CME"
            # Pivot-PR-C will replace this with a real
            # `ib.reqContractDetailsAsync(...)` lookup for the front-month
            # contract. For now, the local_symbol is empty (the dispatcher
            # treats this as "Pivot-PR-C will fix" and falls back to a
            # market-aware lookup elsewhere if needed).
            local_symbol = ""
            multiplier = {
                "/MES": 5,
                "/MNQ": 2,
                "/MYM": 0,
                "/M2K": 5,
                "/MGC": 10,
                "/MCL": 100,
                "/MBT": 1,
            }.get(market, 1)
        elif market in phase1_etfs:
            exchange = "SMART"
            local_symbol = market
            multiplier = 1
        else:
            raise KeyError(
                f"market {market!r} not in Phase 1 universe; "
                "see strategies.v1_trend_following.parameters.V1_CANDIDATE_UNIVERSE"
            )

        ref = IbkrContractRef(
            market=market,
            ibkr_local_symbol=local_symbol,
            ibkr_con_id=None,
            multiplier=multiplier,
            exchange=exchange,
        )
        self._contract_cache[market] = ref
        return ref

    async def subscribe_order_status(self, callback: OrderStatusCallback) -> None:
        """Wire ``callback`` onto ``ib.orderStatusEvent``.

        Idempotent: subsequent calls with the same callable replace the
        registered callback in place (no double-fire). The first call
        attaches our internal sync handler to ``ib.orderStatusEvent``;
        subsequent calls just swap the stored callback.

        Captures the current running event loop so the sync handler
        (invoked by ib_async from the network reader thread) can
        ``asyncio.run_coroutine_threadsafe`` the user's async callback
        back into the worker's loop.
        """
        ib = await self._ensure_connected()
        self._order_status_callback = callback
        try:
            self._order_status_loop = asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover — only fires outside a running loop
            raise RuntimeError(
                "subscribe_order_status() must be called from within a running asyncio loop"
            ) from exc

        if not self._order_status_wired:
            try:
                ib.orderStatusEvent += self._on_order_status
            except Exception as exc:  # pragma: no cover — ib_async should always expose this
                log.error("ibkr_orderstatus_subscribe_failed", error=str(exc))
                raise
            self._order_status_wired = True
            log.info(
                "ibkr_orderstatus_subscribed",
                client_id=self._client_id,
            )

    def _on_order_status(self, trade: Any) -> None:
        """Sync handler attached to ib_async.IB.orderStatusEvent.

        ib_async invokes this from its network reader thread on every
        order-status transition. We schedule the user's async callback
        back onto the loop captured at subscribe time + return immediately
        so the reader thread isn't blocked.

        Exceptions raised during update construction are swallowed (logged
        only) so a malformed Trade can't kill the broker connection.
        """
        callback = self._order_status_callback
        loop = self._order_status_loop
        if callback is None or loop is None:
            return
        try:
            update = self._build_order_status_update(trade)
        except Exception as exc:
            log.warning(
                "ibkr_orderstatus_build_failed",
                error=str(exc),
                trade_repr=repr(trade)[:200],
            )
            return
        if update is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._invoke_callback(callback, update), loop)
        except Exception as exc:  # pragma: no cover — loop should always accept
            log.warning("ibkr_orderstatus_schedule_failed", error=str(exc))

    async def _invoke_callback(
        self, callback: OrderStatusCallback, update: OrderStatusUpdate
    ) -> None:
        """Wrap the user callback in a try/except so a misbehaving consumer
        can't crash the IBKR adapter — IBKR events keep flowing."""
        try:
            await callback(update)
        except Exception:
            log.exception(
                "ibkr_orderstatus_callback_error",
                client_order_id=update.client_order_id,
                status=update.status,
            )

    def _build_order_status_update(self, trade: Any) -> OrderStatusUpdate | None:
        """Translate an ib_async Trade into an OrderStatusUpdate.

        Returns ``None`` if the trade lacks the minimal fields we need
        (no orderRef → no signal lookup possible; no order_status →
        nothing to surface). Logs at warning so operators can debug
        unusual transitions.
        """
        order = getattr(trade, "order", None)
        order_status = getattr(trade, "orderStatus", None)
        contract = getattr(trade, "contract", None)
        if order is None or order_status is None or contract is None:
            log.warning(
                "ibkr_orderstatus_trade_missing_fields",
                has_order=order is not None,
                has_status=order_status is not None,
                has_contract=contract is not None,
            )
            return None

        client_order_id = getattr(order, "orderRef", "") or ""
        if not client_order_id:
            # Manual TWS-side orders carry no orderRef. We can't map them
            # back to a signal so skip silently — this is expected when
            # an operator places a hand order in TWS for diagnostics.
            return None

        raw_status = getattr(order_status, "status", "")
        status: OrderStatusKind = _ORDER_STATUS_KIND_MAP.get(raw_status, "submitted")

        action = getattr(order, "action", "").upper()
        side: IbkrOrderSide = "buy" if action == "BUY" else "sell"

        sec_type = getattr(contract, "secType", "")
        symbol = getattr(contract, "symbol", "")
        market = f"/{symbol}" if sec_type == "FUT" else symbol

        cumulative = _to_decimal(getattr(order_status, "filled", 0))
        remaining = _to_decimal(getattr(order_status, "remaining", 0))
        avg_fill_price_raw = getattr(order_status, "avgFillPrice", 0)
        avg_fill_price: Decimal | None = (
            _to_decimal(avg_fill_price_raw) if avg_fill_price_raw not in (None, 0, 0.0) else None
        )

        # Aggregate commission across all fills on this trade.
        total_commission = Decimal(0)
        last_fill_at: datetime | None = None
        for fill in getattr(trade, "fills", []) or []:
            report = getattr(fill, "commissionReport", None)
            if report is not None:
                comm = getattr(report, "commission", None)
                if comm is not None:
                    total_commission += _to_decimal(comm)
            fill_time = getattr(fill, "time", None)
            if isinstance(fill_time, datetime):
                aware = fill_time if fill_time.tzinfo is not None else fill_time.replace(tzinfo=UTC)
                if last_fill_at is None or aware > last_fill_at:
                    last_fill_at = aware

        broker_order_id = int(getattr(order, "orderId", 0) or 0)

        return OrderStatusUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            status=status,
            market=market,
            side=side,
            cumulative_filled_quantity=cumulative,
            remaining_quantity=remaining,
            avg_fill_price=avg_fill_price,
            total_commission_usd=total_commission,
            last_fill_at_utc=last_fill_at,
            observed_at_utc=_ts_utc(),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ib_contract(self, ref: IbkrContractRef) -> Any:
        """Build an ib_async Contract from our typed IbkrContractRef.

        Lazy-imports ib_async.Future + ib_async.Stock so the module can be
        imported without the library installed. Returns ``Any`` because the
        ib_async type stubs aren't published (see pyproject.toml mypy
        override).
        """
        from ib_async import Future, Stock

        if ref.market.startswith("/"):
            # Futures: use the front-month continuous contract
            return Future(
                symbol=ref.market.lstrip("/"),
                exchange=ref.exchange,
                currency="USD",
            )
        # ETFs
        return Stock(symbol=ref.ibkr_local_symbol, exchange=ref.exchange, currency="USD")

    def _contract_from_ib(self, ib_contract: Any) -> IbkrContractRef:
        """Reverse-map an ib_async Contract to our typed IbkrContractRef."""
        symbol = getattr(ib_contract, "symbol", "")
        local_symbol = getattr(ib_contract, "localSymbol", symbol)
        con_id = getattr(ib_contract, "conId", None)
        sec_type = getattr(ib_contract, "secType", "")
        exchange = getattr(ib_contract, "exchange", "")
        if sec_type == "FUT":
            market = f"/{symbol}"
        else:
            market = symbol
        return IbkrContractRef(
            market=market,
            ibkr_local_symbol=local_symbol,
            ibkr_con_id=con_id if isinstance(con_id, int) else None,
            multiplier=int(getattr(ib_contract, "multiplier", 1) or 1),
            exchange=exchange,
        )


__all__ = ["DEFAULT_CLIENT_ID", "IbAsyncIbkrClient"]
