"""services/webhook_pusher/event_pushes.py — non-alert Discord channel pushes.

Pivot-PR-E (post-pivot 2026-05-12). Adds Discord embed builders + a
dispatch planner for the `#signals` + `#fills` channel-push paths per
frontend-spec §6.1. These complement the existing alert path in
``payloads.py`` (P0/P1/P2 → `#alerts`/`#critical`) — alerts surface
operational problems; event pushes surface successful business state
transitions (a signal was emitted; an order filled).

**Architectural integration:**

The end-to-end pipeline (when wired in a follow-up):

  1. LEAN Local emits ``signal_emitted`` via ``POST /api/internal/lean/signals``
     (Pivot-PR-A endpoint + future Pivot-PR-D-extension for real signal payloads)
  2. Backend dispatcher writes audit row + signals row
  3. Backend emits ``signal_emitted`` SSE envelope to web clients
  4. Background worker (this PR's planner shape; wiring is post-PR-E)
     consumes the SSE envelope OR polls signals/orders/fills tables +
     dispatches via this module's ``plan_event_push(...)`` to webhook_pusher

For PR-E scope, this module ships:
* Pure-function embed builders mirroring the Day 10 alert-embed pattern
* A ``plan_event_push`` planner returning an ``EventPushPlan`` (no I/O)
* A ``dispatch_event_push`` orchestrator that POSTs to Discord via the
  existing ``services.webhook_pusher.sender.post_outbound_message``

A02 N/A — this module is in `services/webhook_pusher/**` (NOT on the
forbidden whitelist; on §2.3 hot-fix whitelist).
A04 enforced — uses existing locked audit `event_type` strings from
``services/audit/event_types.py``; new event types require enum
migration + this module update.
A06 enforced — every datetime tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

# Color tokens mirror the Day 23 services/discord_bot/embeds.py palette
# (24-bit RGB integers per Discord embed API).
COLOR_SIGNAL_EMITTED = 0x3498DB  # blue — informational; awaiting operator action
COLOR_SIGNAL_APPROVED = 0x2ECC71  # green — operator approved
COLOR_SIGNAL_REJECTED = 0x95A5A6  # gray — operator rejected
COLOR_SIGNAL_DEFERRED = 0xF39C12  # amber — operator deferred
COLOR_ORDER_FILLED = 0x2ECC71  # green — broker confirmed fill
COLOR_ORDER_REJECTED = 0xE74C3C  # red — broker rejected order


_ET = ZoneInfo("America/New_York")


class EventChannel(StrEnum):
    """Discord channels that receive non-alert event pushes."""

    SIGNALS = "signals"
    FILLS = "fills"


@dataclass(frozen=True, slots=True)
class SignalEmittedEvent:
    """Payload subset for the ``signal_emitted`` embed."""

    signal_id: str  # UUID-as-string
    market: str  # e.g., "/MES" or "TLT"
    direction: str  # "long" or "short"
    target_contracts: int
    decision_price: Decimal
    emitted_at_utc: datetime
    environment: str  # "paper" | "live-small" | "live-scale"
    strategy_version: str  # 7-char strategy_hash prefix
    anomaly_reasons: tuple[str, ...] = ()  # populated when sizing trace flagged anomalies


@dataclass(frozen=True, slots=True)
class SignalDecisionEvent:
    """Payload subset for ``signal_approved`` / ``signal_rejected`` / ``signal_deferred``."""

    signal_id: str
    market: str
    direction: str
    decision: str  # "approved" | "rejected" | "deferred"
    decided_at_utc: datetime
    decided_by_user_id: str
    environment: str
    diary_reasoning_text: str | None = None  # populated for reject/defer


@dataclass(frozen=True, slots=True)
class OrderFilledEvent:
    """Payload subset for the ``order_filled`` embed."""

    order_id: str  # broker order ID
    client_order_id: str
    signal_id: str  # UUID-as-string for /trades/:id deep-link
    market: str
    side: str  # "buy" | "sell"
    quantity: Decimal
    fill_price: Decimal
    commission_usd: Decimal
    filled_at_utc: datetime
    environment: str


@dataclass(frozen=True, slots=True)
class EventPushPlan:
    """Output of :func:`plan_event_push` — embed payload + target channel + dedupe key.

    The dispatcher consumes this to POST to the Discord webhook URL for
    the target channel. ``dedupe_key`` lets the dispatcher short-circuit
    if the same event already delivered (similar to alerts.idempotency).
    """

    channel: EventChannel
    embed: dict[str, Any]  # Discord embed JSON object
    dedupe_key: str  # e.g., "signal_emitted:<signal_id>" or "order_filled:<order_id>"


def _format_et(ts_utc: datetime) -> str:
    """Convert tz-aware UTC datetime → 'HH:MM ET' formatted string.

    Raises ValueError on naive datetime per [A06].
    """
    if ts_utc.tzinfo is None:
        raise ValueError("ts_utc must be tz-aware UTC; got naive datetime")
    return ts_utc.astimezone(_ET).strftime("%H:%M ET")


def _format_et_date(ts_utc: datetime) -> str:
    """Convert tz-aware UTC datetime → 'YYYY-MM-DD HH:MM ET'."""
    if ts_utc.tzinfo is None:
        raise ValueError("ts_utc must be tz-aware UTC; got naive datetime")
    return ts_utc.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET")


def build_signal_emitted_embed(event: SignalEmittedEvent) -> dict[str, Any]:
    """Build the Discord embed for a freshly-emitted signal.

    Embed shape matches Discord's embed API spec (Object < 6000 chars).
    Anomaly badge appended when ``anomaly_reasons`` is non-empty.

    Deep-link convention: title links to ``/trades/<signal_id>`` per
    frontend-spec §6.3 (Discord deep-links resolve to the trade-detail
    page).
    """
    title = f"{event.direction.upper()} signal: {event.market}"
    if event.anomaly_reasons:
        title = f"⚠️ {title} (anomaly)"

    fields = [
        {"name": "Direction", "value": event.direction, "inline": True},
        {
            "name": "Size",
            "value": f"{event.target_contracts} contracts",
            "inline": True,
        },
        {
            "name": "Decision Price",
            "value": f"${event.decision_price}",
            "inline": True,
        },
        {
            "name": "Emitted",
            "value": _format_et(event.emitted_at_utc),
            "inline": True,
        },
        {"name": "Strategy", "value": event.strategy_version, "inline": True},
        {"name": "Env", "value": event.environment, "inline": True},
    ]
    if event.anomaly_reasons:
        fields.append(
            {
                "name": "Anomaly reasons",
                "value": ", ".join(event.anomaly_reasons),
                "inline": False,
            }
        )

    return {
        "title": title,
        "color": COLOR_SIGNAL_EMITTED,
        "fields": fields,
        "footer": {
            "text": (f"signal_id: {event.signal_id} | awaiting operator approval on the Today page")
        },
        "timestamp": event.emitted_at_utc.isoformat(),
    }


def build_signal_decision_embed(event: SignalDecisionEvent) -> dict[str, Any]:
    """Build the Discord embed for an approve/reject/defer decision."""
    decision_lc = event.decision.lower()
    if decision_lc == "approved":
        color = COLOR_SIGNAL_APPROVED
        title = f"✓ APPROVED: {event.direction.upper()} {event.market}"
    elif decision_lc == "rejected":
        color = COLOR_SIGNAL_REJECTED
        title = f"✗ REJECTED: {event.direction.upper()} {event.market}"
    elif decision_lc == "deferred":
        color = COLOR_SIGNAL_DEFERRED
        title = f"~ DEFERRED: {event.direction.upper()} {event.market}"
    else:
        raise ValueError(f"unsupported decision: {event.decision!r}")

    fields: list[dict[str, Any]] = [
        {
            "name": "Decided",
            "value": _format_et(event.decided_at_utc),
            "inline": True,
        },
        {"name": "By", "value": event.decided_by_user_id, "inline": True},
        {"name": "Env", "value": event.environment, "inline": True},
    ]
    if event.diary_reasoning_text:
        # Truncate to 1024 (Discord field-value max).
        reasoning = event.diary_reasoning_text[:1020] + (
            "…" if len(event.diary_reasoning_text) > 1020 else ""
        )
        fields.append({"name": "Reasoning", "value": reasoning, "inline": False})

    return {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"signal_id: {event.signal_id}"},
        "timestamp": event.decided_at_utc.isoformat(),
    }


def build_order_filled_embed(event: OrderFilledEvent) -> dict[str, Any]:
    """Build the Discord embed for a confirmed order fill."""
    notional = event.quantity * event.fill_price
    return {
        "title": f"✓ FILLED: {event.side.upper()} {event.quantity} {event.market} @ ${event.fill_price}",
        "color": COLOR_ORDER_FILLED,
        "fields": [
            {"name": "Side", "value": event.side, "inline": True},
            {"name": "Qty", "value": str(event.quantity), "inline": True},
            {
                "name": "Fill Price",
                "value": f"${event.fill_price}",
                "inline": True,
            },
            {"name": "Notional", "value": f"${notional}", "inline": True},
            {
                "name": "Commission",
                "value": f"${event.commission_usd}",
                "inline": True,
            },
            {"name": "Env", "value": event.environment, "inline": True},
            {
                "name": "Filled",
                "value": _format_et_date(event.filled_at_utc),
                "inline": False,
            },
        ],
        "footer": {
            "text": (
                f"order_id: {event.client_order_id} | "
                f"signal_id: {event.signal_id} | "
                f"broker_order_id: {event.order_id}"
            )
        },
        "timestamp": event.filled_at_utc.isoformat(),
    }


def plan_event_push(
    *,
    channel: EventChannel,
    event: SignalEmittedEvent | SignalDecisionEvent | OrderFilledEvent,
) -> EventPushPlan:
    """Build the dispatch plan for a single event push.

    Routing:
        SignalEmittedEvent or SignalDecisionEvent → EventChannel.SIGNALS
        OrderFilledEvent                          → EventChannel.FILLS

    The caller passes the channel explicitly so the routing logic stays
    obvious; this function validates the (channel, event) pair is sane.
    """
    if isinstance(event, SignalEmittedEvent):
        if channel != EventChannel.SIGNALS:
            raise ValueError("SignalEmittedEvent must route to EventChannel.SIGNALS")
        embed = build_signal_emitted_embed(event)
        dedupe_key = f"signal_emitted:{event.signal_id}"
    elif isinstance(event, SignalDecisionEvent):
        if channel != EventChannel.SIGNALS:
            raise ValueError("SignalDecisionEvent must route to EventChannel.SIGNALS")
        embed = build_signal_decision_embed(event)
        dedupe_key = f"signal_{event.decision.lower()}:{event.signal_id}"
    elif isinstance(event, OrderFilledEvent):
        if channel != EventChannel.FILLS:
            raise ValueError("OrderFilledEvent must route to EventChannel.FILLS")
        embed = build_order_filled_embed(event)
        dedupe_key = f"order_filled:{event.order_id}"
    else:
        raise TypeError(f"unsupported event type: {type(event).__name__}")

    return EventPushPlan(channel=channel, embed=embed, dedupe_key=dedupe_key)


__all__ = [
    "COLOR_ORDER_FILLED",
    "COLOR_ORDER_REJECTED",
    "COLOR_SIGNAL_APPROVED",
    "COLOR_SIGNAL_DEFERRED",
    "COLOR_SIGNAL_EMITTED",
    "COLOR_SIGNAL_REJECTED",
    "EventChannel",
    "EventPushPlan",
    "OrderFilledEvent",
    "SignalDecisionEvent",
    "SignalEmittedEvent",
    "build_order_filled_embed",
    "build_signal_decision_embed",
    "build_signal_emitted_embed",
    "plan_event_push",
]
