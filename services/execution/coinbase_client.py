"""services/execution/coinbase_client.py — Coinbase Advanced Trade broker transport.

Crypto-pivot C0-B2b (delta spec §3.1). The authenticated transport
layer under the execution policy in ``coinbase_adapter.py``:

* :class:`CoinbaseBrokerClient` — the async Protocol the policy layer
  (and, in C0-B3+, the strategy worker / recon fetcher) programs
  against. Tests inject fakes at this seam.
* :class:`SdkCoinbaseBrokerClient` — the concrete implementation over
  ``coinbase-advanced-py``'s :class:`coinbase.rest.RESTClient`. The SDK
  is synchronous (requests-based, JWT ES256 signing); every call is
  pushed off the event loop via ``asyncio.to_thread``. The SDK is
  imported lazily so this module imports on hosts without it.
* Pure parser functions (``parse_order_state`` / ``parse_fill`` /
  ``parse_position`` / ``parse_balance_summary`` / ``parse_perp_product_ref``)
  converting raw venue payloads → the venue-neutral DTOs in
  ``services/execution/types.py``. All money/rates through ``Decimal``
  at this boundary ([A05]).

Design contracts (mirroring the IBKR-era client the pivot retired):

* Per-order rejections are **data** (``BrokerOrderAck.accepted=False``)
  — the ladder needs the rejection to decide its next stage. Transport
  failures raise :class:`BrokerError` and the caller must re-reconcile
  by ``client_order_id`` before retrying (deterministic IDs make that
  safe).
* **No intraday-margin opt-in, by construction**: this Protocol does
  not expose ``set_intraday_margin_setting`` (strategy §7 locked rule;
  delta spec §3.1 "Do not opt in"). The system runs the overnight
  margin regime 24/7. Re-adding that surface is a strategy amendment,
  not a refactor.

A02 BINDS — ``services/execution/**`` forbidden path;
``risk-review-approved`` required.
A13 (revised 2026-07-08) — DO use ``coinbase-advanced-py`` against CDE
products from here; DO NOT re-introduce IBKR/LEAN/QC paths; DO NOT
hardcode CDE product IDs.
A27 — operator smoke runbook at ``deploy/coinbase_execution/README.md``
(1-contract canary ladder/stop/restart drills = the C0 offline exit
gates, operator-run at go-live).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

import structlog

from services.execution.types import (
    BestBidAsk,
    BrokerError,
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerPosition,
    FuturesBalanceSummary,
    OrderKind,
    OrderSide,
    PerpProductRef,
)

log = structlog.get_logger()

#: Production REST host (SDK default). Overridable for sandbox drills.
DEFAULT_API_HOST: Final[str] = "api.coinbase.com"

#: Venue → locked status mapping. Unknown venue statuses map to
#: ``unknown`` + a WARNING at the boundary (never a crash — a venue
#: enum addition must not take down the execution path).
_STATUS_MAP: Final[dict[str, BrokerOrderStatus]] = {
    "OPEN": "open",
    "PENDING": "queued",
    "QUEUED": "queued",
    "CANCEL_QUEUED": "open",
    "FILLED": "filled",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
    "FAILED": "failed",
    "REJECTED": "rejected",
}


# --------------------------------------------------------------------------
# boundary coercion helpers (pure)
# --------------------------------------------------------------------------


def _dec(value: object) -> Decimal | None:
    """Boundary Decimal coercion ([A05]); None on missing/blank/garbage.

    Accepts plain scalars AND the ``{"value": "...", "currency": "USD"}``
    money objects the futures balance summary nests.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = value.get("value")
        if value is None:
            return None
    try:
        s = str(value).strip()
        if not s:
            return None
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _dec0(value: object) -> Decimal:
    """Like :func:`_dec` but defaulting to 0 (fees/filled-size counters)."""
    d = _dec(value)
    return d if d is not None else Decimal(0)


def _side(value: object) -> OrderSide:
    s = str(value or "").strip().lower()
    if s in ("buy", "long"):
        return "buy"
    if s in ("sell", "short"):
        return "sell"
    raise ValueError(f"unrecognized order side: {value!r}")


def _ts(value: object, *, fallback: datetime | None = None) -> datetime:
    """Parse a venue RFC-3339 timestamp; tz-aware UTC out ([A06])."""
    if value:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts.astimezone(UTC)
        except ValueError:
            pass
    return fallback if fallback is not None else datetime.now(tz=UTC)


def parse_order_state(raw: Mapping[str, Any]) -> BrokerOrderState:
    """Parse one Get/List Orders entry into a :class:`BrokerOrderState`."""
    status_raw = str(raw.get("status") or "").upper()
    status = _STATUS_MAP.get(status_raw)
    if status is None:
        log.warning("coinbase_order_status_unrecognized", status=status_raw)
        status = "unknown"

    config = raw.get("order_configuration")
    config_map: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
    kind: OrderKind | None = None
    contracts = Decimal(0)
    limit_like: Mapping[str, Any] = {}
    if "limit_limit_gtc" in config_map:
        limit_like = config_map["limit_limit_gtc"] or {}
        kind = "limit_post_only" if bool(limit_like.get("post_only")) else "limit_ioc"
    elif "limit_limit_ioc" in config_map:
        limit_like = config_map["limit_limit_ioc"] or {}
        kind = "limit_ioc"
    elif "sor_limit_ioc" in config_map:
        limit_like = config_map["sor_limit_ioc"] or {}
        kind = "limit_ioc"
    elif "market_market_ioc" in config_map:
        limit_like = config_map["market_market_ioc"] or {}
        kind = "market"
    elif "stop_limit_stop_limit_gtc" in config_map:
        limit_like = config_map["stop_limit_stop_limit_gtc"] or {}
        kind = "stop_limit"
    contracts = _dec0(limit_like.get("base_size"))

    return BrokerOrderState(
        order_id=str(raw.get("order_id") or ""),
        client_order_id=str(raw.get("client_order_id") or ""),
        product_id=str(raw.get("product_id") or ""),
        side=_side(raw.get("side")),
        status=status,
        contracts=contracts,
        filled_contracts=_dec0(raw.get("filled_size")),
        avg_fill_price=_dec(raw.get("average_filled_price")),
        total_fees_usd=_dec0(raw.get("total_fees")),
        kind=kind,
        stop_trigger_price=_dec(limit_like.get("stop_price")),
        raw=dict(raw),
    )


def parse_fill(raw: Mapping[str, Any]) -> BrokerFill:
    """Parse one Get Fills entry."""
    return BrokerFill(
        fill_id=str(raw.get("entry_id") or raw.get("trade_id") or ""),
        order_id=str(raw.get("order_id") or ""),
        client_order_id=(str(raw["client_order_id"]) if raw.get("client_order_id") else None),
        product_id=str(raw.get("product_id") or ""),
        side=_side(raw.get("side")),
        contracts=_dec0(raw.get("size")),
        price=_dec0(raw.get("price")),
        fee_usd=_dec0(raw.get("commission")),
        filled_at_utc=_ts(raw.get("trade_time")),
    )


def parse_position(raw: Mapping[str, Any]) -> BrokerPosition:
    """Parse one List Futures Positions entry; sign per venue side."""
    count = _dec0(raw.get("number_of_contracts"))
    side_raw = str(raw.get("side") or "").upper()
    signed = -count if side_raw in ("SHORT", "FUTURES_POSITION_SIDE_SHORT") else count
    return BrokerPosition(
        product_id=str(raw.get("product_id") or ""),
        contracts=signed,
        entry_vwap=_dec(raw.get("avg_entry_price")),
        mark_price=_dec(raw.get("current_price")),
        unrealized_pnl_usd=_dec(raw.get("unrealized_pnl")),
        raw=dict(raw),
    )


def parse_balance_summary(
    raw: Mapping[str, Any], *, snapshot_at_utc: datetime
) -> FuturesBalanceSummary:
    """Parse the Get Futures Balance Summary payload."""
    return FuturesBalanceSummary(
        total_usd_balance=_dec(raw.get("total_usd_balance")),
        cbi_usd_balance=_dec(raw.get("cbi_usd_balance")),
        cfm_usd_balance=_dec(raw.get("cfm_usd_balance")),
        available_margin=_dec(raw.get("available_margin")),
        initial_margin=_dec(raw.get("initial_margin")),
        unrealized_pnl=_dec(raw.get("unrealized_pnl")),
        daily_realized_pnl=_dec(raw.get("daily_realized_pnl")),
        liquidation_threshold=_dec(raw.get("liquidation_threshold")),
        liquidation_buffer_amount=_dec(raw.get("liquidation_buffer_amount")),
        liquidation_buffer_percentage=_dec(raw.get("liquidation_buffer_percentage")),
        snapshot_at_utc=snapshot_at_utc,
        raw=dict(raw),
    )


def parse_perp_product_ref(raw: Mapping[str, Any]) -> PerpProductRef | None:
    """Classify + parse one products entry as a tradable CDE perp ref.

    Reuses the market-data layer's structural classifier
    (:func:`services.data.coinbase_market_data.parse_perp_product`) so
    the trading side and the telemetry side can never disagree about
    what counts as a CDE perp product.
    """
    from services.data.coinbase_market_data import parse_perp_product

    snap = parse_perp_product(raw)
    if snap is None:
        return None
    if snap.contract_size is None or snap.tick_size is None:
        log.warning(
            "coinbase_perp_product_missing_spec",
            product_id=snap.product_id,
            has_contract_size=snap.contract_size is not None,
            has_tick_size=snap.tick_size is not None,
        )
        return None
    base = str(
        raw.get("base_display_symbol")
        or raw.get("base_currency_id")
        or (raw.get("future_product_details") or {}).get("contract_root_unit")
        or ""
    ).upper()
    if not base:
        log.warning("coinbase_perp_product_missing_base_asset", product_id=snap.product_id)
        return None
    return PerpProductRef(
        product_id=snap.product_id,
        base_asset=base,
        contract_size=snap.contract_size,
        tick_size=snap.tick_size,
        trading_disabled=bool(snap.trading_disabled),
        raw=dict(raw),
    )


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------


class CoinbaseBrokerClient(Protocol):
    """Async Protocol for the authenticated Coinbase broker transport.

    Deliberately narrow: exactly the surface §3.1 and the §5 execution
    ladder need. There is NO ``set_intraday_margin_setting`` here — see
    module docstring.
    """

    async def list_perp_products(self) -> list[PerpProductRef]:
        """Runtime discovery of tradable CDE perp-style products ([A13])."""
        ...

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        """Top-of-book for the ladder's stage-1 join / stage-2 mid."""
        ...

    async def create_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        """Place one order. Rejection is data; transport failure raises."""
        ...

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        """Submit cancels; venue confirms async. Returns per-id accept flag."""
        ...

    async def get_order(self, order_id: str) -> BrokerOrderState:
        """Fresh snapshot of one order."""
        ...

    async def list_open_orders(self, product_id: str | None = None) -> list[BrokerOrderState]:
        """All working orders (optionally per product) — stop verification (gate A2)."""
        ...

    async def find_order_by_client_id(
        self, product_id: str, client_order_id: str
    ) -> BrokerOrderState | None:
        """Resolve a deterministic client_order_id → existing venue order.

        The idempotent-crash-recovery primitive: on a duplicate-ID
        rejection (or after a restart), the caller looks the original
        order up instead of re-placing.
        """
        ...

    async def list_fills(
        self,
        *,
        product_id: str | None = None,
        order_ids: list[str] | None = None,
        start_utc: datetime | None = None,
    ) -> list[BrokerFill]:
        """Execution legs for P&L/recon (gate A1)."""
        ...

    async def list_positions(self) -> list[BrokerPosition]:
        """Current CFM futures positions (restart recovery + recon)."""
        ...

    async def get_futures_balance_summary(self) -> FuturesBalanceSummary:
        """Equity/margin snapshot (sizing input + liquidation buffer)."""
        ...


# --------------------------------------------------------------------------
# SDK-backed implementation
# --------------------------------------------------------------------------


class SdkCoinbaseBrokerClient:
    """``coinbase-advanced-py``-backed :class:`CoinbaseBrokerClient`.

    The SDK is synchronous; every venue call runs in a worker thread
    via ``asyncio.to_thread`` with a hard ``timeout_s`` deadline. SDK
    responses are converted to plain dicts (``to_dict()``) and parsed
    by the pure functions above.
    """

    def __init__(
        self,
        *,
        api_key_name: str,
        api_private_key: str,
        api_host: str = DEFAULT_API_HOST,
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key_name = api_key_name
        self._api_private_key = api_private_key
        self._api_host = api_host
        self._timeout_s = timeout_s
        self._rest: Any | None = None
        self._log = log.bind(client="coinbase_broker", api_host=api_host)

    def _rest_client(self) -> Any:
        if self._rest is None:
            # Lazy import: hosts without the SDK can import this module.
            from coinbase.rest import RESTClient

            self._rest = RESTClient(
                api_key=self._api_key_name,
                api_secret=self._api_private_key,
                base_url=self._api_host,
                timeout=int(self._timeout_s),
            )
        return self._rest

    async def _call(self, operation: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs), timeout=self._timeout_s
            )
        except TimeoutError as exc:
            raise BrokerError(
                operation=operation,
                detail=f"venue call exceeded {self._timeout_s}s",
                underlying_exception_class="TimeoutError",
                occurred_at_utc=datetime.now(tz=UTC),
            ) from exc
        except Exception as exc:
            raise BrokerError(
                operation=operation,
                detail=repr(exc),
                underlying_exception_class=type(exc).__name__,
                occurred_at_utc=datetime.now(tz=UTC),
            ) from exc

    @staticmethod
    def _to_dict(obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            return obj
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            result = to_dict()
            if isinstance(result, dict):
                return result
        raise BrokerError(
            operation="to_dict",
            detail=f"unparseable SDK response type {type(obj).__name__}",
            underlying_exception_class="TypeError",
            occurred_at_utc=datetime.now(tz=UTC),
        )

    # -- discovery ----------------------------------------------------------

    async def list_perp_products(self) -> list[PerpProductRef]:
        rest = self._rest_client()
        body = self._to_dict(
            await self._call(
                "list_perp_products",
                rest.get_products,
                product_type="FUTURE",
                get_all_products=True,
            )
        )
        products = body.get("products") or []
        refs: list[PerpProductRef] = []
        for raw in products:
            if isinstance(raw, Mapping):
                ref = parse_perp_product_ref(raw)
                if ref is not None:
                    refs.append(ref)
        return refs

    async def get_best_bid_ask(self, product_id: str) -> BestBidAsk:
        rest = self._rest_client()
        body = self._to_dict(
            await self._call("get_best_bid_ask", rest.get_best_bid_ask, [product_id])
        )
        books = body.get("pricebooks") or []
        for book in books:
            if not isinstance(book, Mapping) or str(book.get("product_id")) != product_id:
                continue
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            bid = _dec(bids[0].get("price")) if bids and isinstance(bids[0], Mapping) else None
            ask = _dec(asks[0].get("price")) if asks and isinstance(asks[0], Mapping) else None
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                return BestBidAsk(
                    product_id=product_id,
                    bid=bid,
                    ask=ask,
                    observed_at_utc=_ts(book.get("time")),
                )
        raise BrokerError(
            operation="get_best_bid_ask",
            detail=f"no two-sided book returned for {product_id}",
            underlying_exception_class="ValueError",
            occurred_at_utc=datetime.now(tz=UTC),
        )

    # -- orders ---------------------------------------------------------------

    async def create_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        rest = self._rest_client()
        side_wire = request.side.upper()
        size = str(request.contracts)
        submitted_at = datetime.now(tz=UTC)

        if request.kind == "limit_post_only":
            if request.limit_price is None:
                raise ValueError("limit_post_only requires limit_price")
            raw = await self._call(
                "create_order",
                rest.limit_order_gtc,
                client_order_id=request.client_order_id,
                product_id=request.product_id,
                side=side_wire,
                base_size=size,
                limit_price=str(request.limit_price),
                post_only=True,
            )
        elif request.kind == "limit_ioc":
            if request.limit_price is None:
                raise ValueError("limit_ioc requires limit_price")
            raw = await self._call(
                "create_order",
                rest.limit_order_ioc,
                client_order_id=request.client_order_id,
                product_id=request.product_id,
                side=side_wire,
                base_size=size,
                limit_price=str(request.limit_price),
            )
        elif request.kind == "market":
            raw = await self._call(
                "create_order",
                rest.market_order,
                client_order_id=request.client_order_id,
                product_id=request.product_id,
                side=side_wire,
                base_size=size,
            )
        elif request.kind == "stop_limit":
            if (
                request.limit_price is None
                or request.stop_trigger_price is None
                or request.stop_direction is None
            ):
                raise ValueError("stop_limit requires limit, trigger and direction")
            stop_direction_wire = (
                "STOP_DIRECTION_STOP_DOWN"
                if request.stop_direction == "stop_down"
                else "STOP_DIRECTION_STOP_UP"
            )
            raw = await self._call(
                "create_order",
                rest.stop_limit_order_gtc,
                client_order_id=request.client_order_id,
                product_id=request.product_id,
                side=side_wire,
                base_size=size,
                limit_price=str(request.limit_price),
                stop_price=str(request.stop_trigger_price),
                stop_direction=stop_direction_wire,
            )
        else:  # pragma: no cover — Literal-exhaustive
            raise ValueError(f"unsupported order kind: {request.kind!r}")

        body = self._to_dict(raw)
        success = bool(body.get("success"))
        if success:
            resp = body.get("success_response") or {}
            order_id = str(resp.get("order_id") or "") if isinstance(resp, Mapping) else ""
            return BrokerOrderAck(
                client_order_id=request.client_order_id,
                order_id=order_id,
                accepted=True,
                rejection_reason=None,
                submitted_at_utc=submitted_at,
            )
        err = body.get("error_response") or {}
        reason_parts = []
        if isinstance(err, Mapping):
            for key in ("error", "preview_failure_reason", "message", "error_details"):
                val = err.get(key)
                if val:
                    reason_parts.append(str(val))
        reason = "; ".join(reason_parts) or "venue rejected order (no detail)"
        self._log.warning(
            "coinbase_order_rejected",
            client_order_id=request.client_order_id,
            product_id=request.product_id,
            kind=request.kind,
            reason=reason,
        )
        return BrokerOrderAck(
            client_order_id=request.client_order_id,
            order_id="",
            accepted=False,
            rejection_reason=reason,
            submitted_at_utc=submitted_at,
        )

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        if not order_ids:
            return {}
        rest = self._rest_client()
        body = self._to_dict(await self._call("cancel_orders", rest.cancel_orders, order_ids))
        results = body.get("results") or []
        out: dict[str, bool] = dict.fromkeys(order_ids, False)
        for entry in results:
            if isinstance(entry, Mapping) and entry.get("order_id"):
                out[str(entry["order_id"])] = bool(entry.get("success"))
        return out

    async def get_order(self, order_id: str) -> BrokerOrderState:
        rest = self._rest_client()
        body = self._to_dict(await self._call("get_order", rest.get_order, order_id))
        order = body.get("order")
        if not isinstance(order, Mapping):
            raise BrokerError(
                operation="get_order",
                detail=f"no order payload for {order_id}",
                underlying_exception_class="KeyError",
                occurred_at_utc=datetime.now(tz=UTC),
            )
        return parse_order_state(order)

    async def list_open_orders(self, product_id: str | None = None) -> list[BrokerOrderState]:
        rest = self._rest_client()
        kwargs: dict[str, Any] = {"order_status": ["OPEN"]}
        if product_id is not None:
            kwargs["product_ids"] = [product_id]
        body = self._to_dict(await self._call("list_open_orders", rest.list_orders, **kwargs))
        orders = body.get("orders") or []
        return [parse_order_state(o) for o in orders if isinstance(o, Mapping)]

    async def find_order_by_client_id(
        self, product_id: str, client_order_id: str
    ) -> BrokerOrderState | None:
        rest = self._rest_client()
        body = self._to_dict(
            await self._call(
                "find_order_by_client_id",
                rest.list_orders,
                product_ids=[product_id],
                limit=250,
            )
        )
        orders = body.get("orders") or []
        for raw in orders:
            if isinstance(raw, Mapping) and str(raw.get("client_order_id")) == client_order_id:
                return parse_order_state(raw)
        return None

    async def list_fills(
        self,
        *,
        product_id: str | None = None,
        order_ids: list[str] | None = None,
        start_utc: datetime | None = None,
    ) -> list[BrokerFill]:
        rest = self._rest_client()
        kwargs: dict[str, Any] = {}
        if product_id is not None:
            kwargs["product_ids"] = [product_id]
        if order_ids:
            kwargs["order_ids"] = order_ids
        if start_utc is not None:
            kwargs["start_sequence_timestamp"] = (
                start_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
            )
        body = self._to_dict(await self._call("list_fills", rest.get_fills, **kwargs))
        fills = body.get("fills") or []
        return [parse_fill(f) for f in fills if isinstance(f, Mapping)]

    async def list_positions(self) -> list[BrokerPosition]:
        rest = self._rest_client()
        body = self._to_dict(await self._call("list_positions", rest.list_futures_positions))
        positions = body.get("positions") or []
        return [parse_position(p) for p in positions if isinstance(p, Mapping)]

    async def get_futures_balance_summary(self) -> FuturesBalanceSummary:
        rest = self._rest_client()
        body = self._to_dict(
            await self._call("get_futures_balance_summary", rest.get_futures_balance_summary)
        )
        summary = body.get("balance_summary")
        if not isinstance(summary, Mapping):
            raise BrokerError(
                operation="get_futures_balance_summary",
                detail="no balance_summary payload",
                underlying_exception_class="KeyError",
                occurred_at_utc=datetime.now(tz=UTC),
            )
        return parse_balance_summary(summary, snapshot_at_utc=datetime.now(tz=UTC))


__all__ = [
    "DEFAULT_API_HOST",
    "CoinbaseBrokerClient",
    "SdkCoinbaseBrokerClient",
    "parse_balance_summary",
    "parse_fill",
    "parse_order_state",
    "parse_perp_product_ref",
    "parse_position",
]
