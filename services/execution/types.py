"""services/execution/types.py — pure-policy dataclasses for the broker layer.

Crypto-pivot C0-B2b (delta spec §3.1, 2026-07-09). This module was the
IBKR DTO set from Pivot-PR-B (2026-05-12); the IBKR/LEAN/CME stack was
decommissioned at C0 start and its ``Ibkr*`` DTOs died with their only
consumers (``ibkr_adapter``/``ibkr_client``/``order_placement_worker``/
``ibkr_intraday`` — deleted in this same PR per the operator's dead-code
mandate). What replaces them is a **venue-neutral** shape set the
Coinbase transport (``coinbase_client.py``) parses into and the
execution policy layer (``coinbase_adapter.py``) consumes — the §3.1
"DTOs reused where broker-agnostic" contract, applied by rebuilding the
DTOs venue-agnostic rather than keeping IBKR-flavored names alive.

A02 BINDS — this file is on the forbidden whitelist
(``services/execution/**``); ``risk-review-approved`` required.
A05 enforced — all money/price fields are ``Decimal``, serialized as
strings at API boundaries.
A06 enforced — all timestamps tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

#: Order side, our-side convention (lowercase; the Coinbase wire uses
#: upper — the transport maps at the boundary).
OrderSide = Literal["buy", "sell"]

#: Order kinds the execution ladder + stop manager place (strategy §5):
#:
#: * ``limit_post_only`` — ladder stage 1: join the touch, maker-only.
#: * ``limit_ioc`` — ladder stage 2: cross at mid ± 5 bps, immediate-or-cancel.
#: * ``market`` — ladder stage 3 + risk-loop flatten path.
#: * ``stop_limit`` — the venue-native 3xATR backstop (§5 Layer 1). CDE
#:   has no native stop-market; a wide-limit stop-limit is the closest
#:   protective order (limit 1% through the trigger).
OrderKind = Literal["limit_post_only", "limit_ioc", "market", "stop_limit"]

#: Trigger direction for ``stop_limit`` orders. ``stop_down`` = trigger
#: when mark falls to/below the trigger (protects longs); ``stop_up``
#: protects shorts.
StopDirection = Literal["stop_down", "stop_up"]

#: Narrow locked status set the transport maps venue statuses onto.
#: (Coinbase surfaces OPEN / FILLED / CANCELLED / EXPIRED / FAILED /
#: QUEUED / CANCEL_QUEUED / PENDING and more; anything unrecognized maps
#: to ``unknown`` and is logged at the boundary.)
BrokerOrderStatus = Literal[
    "open",
    "filled",
    "cancelled",
    "expired",
    "failed",
    "rejected",
    "queued",
    "unknown",
]

#: Which ladder stage produced a fill / where execution stopped.
LadderStage = Literal["post_only", "ioc_cross", "market"]


@dataclass(frozen=True, slots=True)
class PerpProductRef:
    """One runtime-discovered CDE perp-style product ([A13]: never hardcoded).

    ``contract_size`` is in base-asset units per contract (nano BTC
    0.01, nano ETH 0.10 — read from the venue, never assumed).
    """

    product_id: str
    base_asset: str  # "BTC" / "ETH" — normalized upper
    contract_size: Decimal
    tick_size: Decimal
    trading_disabled: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BrokerOrderRequest:
    """One order the policy layer asks the transport to place.

    ``client_order_id`` is the deterministic hash ID (§3.1) — same
    inputs always produce the same ID so crash recovery is idempotent:
    a duplicate placement attempt is rejected by the venue and resolved
    by looking the original order up instead of double-ordering.
    """

    client_order_id: str
    product_id: str
    side: OrderSide
    contracts: Decimal  # positive count; side carries direction
    kind: OrderKind
    limit_price: Decimal | None = None  # required for both limit kinds + stop_limit
    stop_trigger_price: Decimal | None = None  # required for stop_limit
    stop_direction: StopDirection | None = None  # required for stop_limit


@dataclass(frozen=True, slots=True)
class BrokerOrderAck:
    """Submit-time outcome. Venue rejection is data, not an exception —
    the ladder treats a post-only rejection (would-cross) as the signal
    to escalate to the next stage. Transport failures raise
    :class:`BrokerError` instead.
    """

    client_order_id: str
    order_id: str  # venue-side UUID; "" when rejected at submit
    accepted: bool
    rejection_reason: str | None
    submitted_at_utc: datetime


@dataclass(frozen=True, slots=True)
class BrokerOrderState:
    """Point-in-time order snapshot from the venue (get/list orders)."""

    order_id: str
    client_order_id: str
    product_id: str
    side: OrderSide
    status: BrokerOrderStatus
    contracts: Decimal  # ordered size
    filled_contracts: Decimal
    avg_fill_price: Decimal | None
    total_fees_usd: Decimal
    kind: OrderKind | None  # None when the venue config shape is unrecognized
    stop_trigger_price: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """True once the venue will never fill more of this order."""
        return self.status in ("filled", "cancelled", "expired", "failed", "rejected")

    @property
    def remaining_contracts(self) -> Decimal:
        return max(self.contracts - self.filled_contracts, Decimal(0))


@dataclass(frozen=True, slots=True)
class BrokerFill:
    """One execution leg (REST fills endpoint or WS user channel).

    ``fill_id`` is the venue-side unique leg ID (dedupe key together
    with ``order_id`` — same discipline the IBKR-era
    ``(client_order_id, exec_id)`` pair enforced).
    """

    fill_id: str
    order_id: str
    client_order_id: str | None
    product_id: str
    side: OrderSide
    contracts: Decimal
    price: Decimal
    fee_usd: Decimal
    filled_at_utc: datetime


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """One venue position snapshot (list/get positions).

    ``contracts`` is signed: positive = long, negative = short —
    normalized from the venue's (side, count) pair at the boundary.
    """

    product_id: str
    contracts: Decimal
    entry_vwap: Decimal | None
    mark_price: Decimal | None
    unrealized_pnl_usd: Decimal | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FuturesBalanceSummary:
    """CFM futures balance summary (equity/margin source, §3.1).

    Field-by-field Decimals where the venue provides them; ``raw``
    carries the full payload for recon + forensic use. ``None`` means
    the venue omitted the field — consumers must treat missing margin
    data as a reason to fail safe, never as zero.
    """

    total_usd_balance: Decimal | None
    cbi_usd_balance: Decimal | None
    cfm_usd_balance: Decimal | None
    available_margin: Decimal | None
    initial_margin: Decimal | None
    unrealized_pnl: Decimal | None
    daily_realized_pnl: Decimal | None
    liquidation_threshold: Decimal | None
    liquidation_buffer_amount: Decimal | None
    liquidation_buffer_percentage: Decimal | None
    snapshot_at_utc: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BestBidAsk:
    """Top-of-book snapshot for one product (ladder stage-1 join price)."""

    product_id: str
    bid: Decimal
    ask: Decimal
    observed_at_utc: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class BrokerError(Exception):
    """Terminal transport failure (network/auth/5xx) — NOT a rejection.

    Mirrors the IBKR-era ``IbkrPlacementError`` contract: per-order
    rejections come back as data (``BrokerOrderAck.accepted=False``);
    this exception means the call never completed at the venue, and the
    caller must treat venue state as unknown (re-reconcile by
    client_order_id before retrying).
    """

    operation: str  # e.g., "create_order", "list_positions"
    detail: str
    underlying_exception_class: str
    occurred_at_utc: datetime
    #: HTTP status of the venue response when the underlying exception
    #: carried one (requests.HTTPError shape), else None. Added for the
    #: 2026-07-16 incident: the order-terminal poll must distinguish a
    #: venue read-after-write 404 (just-placed order not yet visible to
    #: GET — retryable within the poll deadline) from every other
    #: transport failure (fail fast, venue state unknown).
    http_status: int | None = None


__all__ = [
    "BestBidAsk",
    "BrokerError",
    "BrokerFill",
    "BrokerOrderAck",
    "BrokerOrderRequest",
    "BrokerOrderState",
    "BrokerOrderStatus",
    "BrokerPosition",
    "FuturesBalanceSummary",
    "LadderStage",
    "OrderKind",
    "OrderSide",
    "PerpProductRef",
    "StopDirection",
]
