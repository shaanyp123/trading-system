"""services/execution/types.py — pure-policy dataclasses for the IBKR client.

Pivot-PR-B (post-pivot 2026-05-12). Broker-agnostic shapes describing the
order / fill / position contract between the backend and the broker. The
``ibkr_adapter.IbAsyncIbkrClient`` translates these into ``ib_async`` calls;
tests use the Protocol from ``ibkr_client.py`` to swap in mocks.

A02 BINDS — this file is on the forbidden whitelist (``services/execution/**``);
`risk-review-approved` required for any future changes.
A05 enforced — all money/price fields are ``Decimal``, serialized as strings
at the API boundary.
A06 enforced — all timestamps tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

#: Locked enum mirroring backend-spec §2.5 (order types).
#:
#: * ``limit_marketable`` — entries with a limit price set 1 tick beyond
#:   the bid/ask to ensure marketability while bounding slippage.
#: * ``stop_market`` — stop orders for exits at the strategy's ATR stop.
#: * ``limit_at_target`` — take-profit limit orders (rarely used in v1
#:   which has no fixed profit target).
#: * ``market`` — emergency-only; defensive trim path uses limit_marketable
#:   1x->2x spread instead. Reserved for kill-switch flatten-all flow.
IbkrOrderType = Literal[
    "limit_marketable",
    "stop_market",
    "limit_at_target",
    "market",
]


#: Locked enum mirroring backend-spec §2.5 (order sides).
IbkrOrderSide = Literal["buy", "sell"]


#: Locked enum mirroring backend-spec §3.4 (orders.status). The IBKR API
#: surfaces a richer status set via ``orderStatus.status``; we map them
#: to this narrower locked set.
IbkrOrderStatus = Literal[
    "pending_submit",
    "submitted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
]


#: Order rejection categories per backend-spec §6.3 (Order Rejection Taxonomy).
#: The adapter maps IBKR error codes to this enum; the dispatcher uses it to
#: decide retry vs. halt.
IbkrRejectionCategory = Literal[
    "insufficient_margin",
    "outside_trading_hours",
    "invalid_contract",
    "duplicate_order",
    "limit_too_far_from_market",
    "broker_disconnect",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class IbkrContractRef:
    """Identifies an IBKR-tradable instrument.

    Phase 1 sub-universe (locked):
        Futures: /MES, /MNQ, /MYM, /M2K, /MGC, /MCL, /MBT
        ETFs:    TLT, IEF, SHY, TIP

    For futures, ``ibkr_local_symbol`` carries the continuous-front contract
    code (e.g., "MESH26" for Mar 2026); the adapter resolves this from the
    market symbol + roll calendar.
    """

    market: str  # e.g., "/MES" or "TLT" — matches strategies parameters.py
    ibkr_local_symbol: str  # IBKR-side instrument symbol (resolves contract)
    ibkr_con_id: int | None = None  # IBKR contract ID; cached after first resolve
    multiplier: int = 1  # futures point multiplier (e.g., 5 for /MES)
    exchange: str = "CME"  # CME for futures; SMART for ETFs


@dataclass(frozen=True, slots=True)
class IbkrPlaceOrderRequest:
    """Request to place a new order via the IBKR adapter.

    The ``client_order_id`` is the 33-char ID per backend-spec §2.5 (locked
    format). It's threaded through as ``orderRef`` on the IBKR side and
    persisted in the ``orders`` table; the same ID is used for the audit
    event UUID generation seed so a single signal → order traceable end-
    to-end.
    """

    client_order_id: str  # 33-char strategy_short-paramset_short-signal_short-retry_n
    contract: IbkrContractRef
    side: IbkrOrderSide
    quantity: Decimal  # number of contracts (futures) or shares (ETFs)
    order_type: IbkrOrderType
    limit_price: Decimal | None = None  # required for limit_marketable / limit_at_target
    stop_price: Decimal | None = None  # required for stop_market
    time_in_force: Literal["DAY", "GTC"] = "DAY"
    parent_client_order_id: str | None = (
        None  # informational: links stop to entry CID; not on the wire
    )
    parent_broker_order_id: int | None = (
        None  # IBKR-side bracket parentId — when set on a stop, IBKR treats it as an OCO child of the entry (entry-cancel auto-cancels stop). PR-gamma / drill 6 defect #3 fix.
    )


@dataclass(frozen=True, slots=True)
class IbkrPlaceOrderResult:
    """Outcome of a placeOrder call.

    ``broker_order_id`` is the IBKR-side numeric ID (returned in
    ``orderStatus.permId``); persisted into the ``orders`` table. The
    operation may succeed locally (order acknowledged by IBKR) but the
    actual fill happens asynchronously — subsequent ``orderStatus`` events
    surface that. Pivot-PR-D's dispatcher subscribes to those event streams.
    """

    client_order_id: str
    broker_order_id: int  # 0 if order was rejected at submit
    status: IbkrOrderStatus
    submitted_at_utc: datetime
    rejection_category: IbkrRejectionCategory | None = None
    rejection_detail: str | None = None  # IBKR's free-text rejection message


@dataclass(frozen=True, slots=True)
class IbkrCancelOrderResult:
    """Outcome of a cancelOrder call.

    IBKR's cancel is async — calling ``cancelOrder`` doesn't immediately
    confirm the cancel; an ``orderStatus`` event with status=Cancelled
    arrives later. This result captures whether the cancel was successfully
    submitted; the actual cancel-confirmed transition is tracked via the
    fill/order-status event subscription.
    """

    client_order_id: str
    broker_order_id: int
    cancel_submitted_at_utc: datetime


@dataclass(frozen=True, slots=True)
class IbkrFill:
    """One execution leg returned by ``IB.fills()`` or the live event stream.

    The composite key ``(client_order_id, exec_id)`` uniquely identifies a
    fill; the dispatcher dedupes by this pair to avoid double-counting if
    the same fill is observed both by the event stream and by the
    end-of-day reconciliation poll.
    """

    client_order_id: str
    broker_order_id: int
    exec_id: str  # IBKR-side execution ID (unique per execution leg)
    contract: IbkrContractRef
    side: IbkrOrderSide
    quantity: Decimal
    fill_price: Decimal
    commission_usd: Decimal
    filled_at_utc: datetime


@dataclass(frozen=True, slots=True)
class IbkrPosition:
    """One position snapshot returned by ``IB.positions()``.

    Captured at intraday recon (60s cadence during CME session, per backend-
    spec §2.6 post-pivot). The dispatcher compares this against the backend's
    ``positions`` table to detect reconciliation breaks.
    """

    contract: IbkrContractRef
    quantity: Decimal  # signed: positive = long; negative = short
    avg_cost_usd: Decimal  # cost basis per contract/share
    market_price_usd: Decimal | None  # last broker-reported mark; None if untradable
    unrealized_pnl_usd: Decimal | None  # None if market_price is None
    realized_pnl_usd: Decimal


@dataclass(frozen=True, slots=True)
class IbkrAccountSummary:
    """Cash + margin snapshot returned by ``IB.accountSummary()``.

    Sampled at 60s recon cadence + on every fill. The risk engine's
    margin-protocol uses ``used_margin_pct = init_margin_usd / available_funds_usd``
    to gate defensive trims.
    """

    account_id: str  # IBKR account number (e.g., "U25655583")
    net_liquidation_usd: Decimal
    available_funds_usd: Decimal
    init_margin_req_usd: Decimal  # initial margin requirement
    maintenance_margin_req_usd: Decimal
    buying_power_usd: Decimal
    snapshot_at_utc: datetime


@dataclass(frozen=True, slots=True)
class IbkrConnectionState:
    """Health snapshot for the ib_gateway TWS API connection.

    ``IB.isConnected()`` is the authoritative reachability check; the
    health-check loop (services/execution/dispatch.py in Pivot-PR-B follow-up,
    or services/monitoring/ in Pivot-PR-C) calls this on a 30s cadence and
    halt-news on consecutive_disconnect > 10 min per backend-spec §6.6.1-alt.
    """

    is_connected: bool
    last_error: str | None
    client_id: int  # the TWS API clientId used by this process
    server_version: int | None  # IBKR TWS protocol version


#: Broker-agnostic surface for one `orderStatusEvent` from the IBKR adapter.
#: The IBKR side surfaces a richer enum (PendingSubmit / Submitted /
#: PreSubmitted / PartiallyFilled / Filled / Cancelled / ApiCancelled /
#: Inactive / ...); the adapter maps those down to this narrower locked
#: set so subscribers don't have to care about the IBKR-specific ones.
#:
#: ``submitted`` collapses PendingSubmit + Submitted + PreSubmitted (the
#: order is live at the broker, no fill yet). ``inactive`` is the
#: post-cancel terminal state IBKR uses for a few odd code paths; we
#: treat it as a no-op from the SSE consumer's perspective.
OrderStatusKind = Literal[
    "submitted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "inactive",
]


@dataclass(frozen=True, slots=True)
class OrderStatusUpdate:
    """One status-event update from the broker's order-status stream.

    Surfaced through ``IbkrClient.subscribe_order_status`` so the
    backend can react to live transitions (Submitted → PartiallyFilled
    → Filled → ...) without polling. The IBKR side fires
    ``orderStatusEvent`` for every transition; the adapter maps each
    raw event to this dataclass before invoking the user callback.

    The ``cumulative_filled_quantity`` + ``avg_fill_price`` +
    ``total_commission_usd`` fields aggregate across all fills observed
    so far on this order — so the consumer of a ``status='filled'``
    update has everything it needs to render the Discord ``#fills``
    embed and the web fills-feed row in one place.

    ``last_fill_at_utc`` is ``None`` for pre-fill statuses (submitted,
    rejected) where the order has no execution legs yet; ``observed_at_utc``
    is always populated as the adapter's receipt timestamp so SSE
    consumers can sort/correlate even without a fill timestamp.
    """

    client_order_id: str  # Trade.order.orderRef — the 33-char client ID
    broker_order_id: int  # Trade.order.orderId
    status: OrderStatusKind  # mapped from Trade.orderStatus.status
    market: str  # derived from Trade.contract (e.g., "/MES" or "TLT")
    side: IbkrOrderSide  # derived from Trade.order.action
    cumulative_filled_quantity: Decimal  # Trade.orderStatus.filled
    remaining_quantity: Decimal  # Trade.orderStatus.remaining
    avg_fill_price: Decimal | None  # Trade.orderStatus.avgFillPrice; None pre-fill
    total_commission_usd: Decimal  # sum across trade.fills[].commissionReport.commission
    last_fill_at_utc: datetime | None  # most recent fill.time; None pre-fill
    observed_at_utc: datetime  # adapter-side receipt timestamp
    rejection_reason: str | None = None
    """IBKR-side error message for terminal-reject events (Defect #2 fix
    2026-05-16). Populated when ``status`` is one of ``cancelled``,
    ``rejected``, or ``inactive`` AND the underlying ib_async ``Trade``
    has a ``log`` entry with a message (the IBKR error code + text).

    For the futures-validation case that surfaced today, this carries
    e.g. ``"321 - Please enter a local symbol or an expiry"`` so the
    backend can stamp ``orders.rejection_reason`` + the
    ``order_rejected`` audit payload's ``rejection_reason`` field.

    None for non-terminal statuses (submitted, filled, partially_filled)
    or when IBKR provides no message on the rejection."""


#: Callback signature for ``IbkrClient.subscribe_order_status``. Async so
#: the implementation can do DB lookups / SSE emits without blocking the
#: ib_async event loop.
OrderStatusCallback = Callable[[OrderStatusUpdate], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class IbkrPlacementError(Exception):
    """Raised on terminal-failure placement attempts (network, auth, etc).

    Different from a per-order rejection (which is captured in the
    ``IbkrPlaceOrderResult.rejection_*`` fields with status=rejected). This
    exception fires when the ENTIRE call couldn't reach IBKR — e.g., the
    ib_gateway container is down, or the TWS API session was disconnected
    mid-call. The dispatcher catches it + signals a defensive_envelope halt
    after consecutive_failures threshold.
    """

    operation: str  # e.g., "placeOrder", "cancelOrder", "reqPositions"
    detail: str
    underlying_exception_class: str  # e.g., "ConnectionError", "TimeoutError"
    occurred_at_utc: datetime
    request_audit_event_uuid: UUID | None = None  # tie-back to the audit row
