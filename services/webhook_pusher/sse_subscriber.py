"""services/webhook_pusher/sse_subscriber.py — long-lived SSE consumer.

Worker-PR-2 follow-up (post-pivot 2026-05-12). Consumes the api's
``/api/sse/events`` stream + routes ``signal`` / ``fill`` envelopes
into Discord channel-push embeds via ``services.webhook_pusher.event_pushes``
+ ``services.webhook_pusher.dispatcher.dispatch_event_push``.

**Why a separate subscriber rather than reusing the api's SSE multiplexer:**
The api's multiplexer fans events out to **browser** subscribers (per-tab
4-connection limit; in-process replay buffer). The webhook_pusher needs
a **server-side** consumer that's:

  * Long-lived (reconnects with Last-Event-ID on drop)
  * Authenticated via shared bearer (reuses the discord-bot bearer)
  * Routing-aware (signal_emitted → #signals, order_filled → #fills)
  * Decoupled from the api process lifecycle

This module is wired into the ``webhook_pusher`` container's entrypoint
(future swap from the Phase 0 ``sleep infinity`` placeholder). Until that
wiring lands, operators can invoke ``python -m services.webhook_pusher.sse_subscriber``
manually for smoke testing.

**Module layout:**

  * :class:`SSEFrame` — parsed envelope (event_type + sequence_no + data).
  * :func:`parse_sse_stream` — async-iterator over an httpx streaming
    response that yields :class:`SSEFrame` objects.
  * :func:`route_event` — pure-function router: an :class:`SSEFrame` →
    a list of ``(channel, event_push_plan)`` pairs. Caller dispatches
    via :func:`services.webhook_pusher.dispatcher.dispatch_event_push`.
  * :class:`SSESubscriber` — long-lived task wrapper: connect, parse,
    route, dispatch, reconnect on disconnect.

**Event-routing matrix** (mapping SSE envelope → event push plan):

  * ``type=signal`` + ``data.action=emitted``  → :class:`SignalEmittedEvent`
  * ``type=signal`` + ``data.action in {approved, rejected, deferred}``
                                               → :class:`SignalDecisionEvent`
  * ``type=fill``   + ``data.event_subtype=order_filled``
                                               → :class:`OrderFilledEvent`

Other event types (position, pnl, alert, health, risk_state, audit,
agent, vacation, watchdog, job, version, session_evicted, ping) are
silently ignored — they're either alert-domain (handled by the existing
``dispatch_alert`` path) or operator-facing only (no Discord push).

**A02** N/A — `services/webhook_pusher/**` is on §2.3 hot-fix whitelist.
**A06** enforced — every datetime tz-aware UTC.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

from services.webhook_pusher.dispatcher import dispatch_event_push
from services.webhook_pusher.event_pushes import (
    EventChannel,
    EventPushPlan,
    OrderFilledEvent,
    OrderPlacedEvent,
    SignalDecisionEvent,
    SignalEmittedEvent,
    plan_event_push,
)

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Frame parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SSEFrame:
    """One parsed SSE envelope from the api stream.

    Mirrors the api-side envelope shape from
    ``services.api.sse._build_envelope`` so callers don't need to know
    the JSON details — they switch on ``event_type`` and consume ``data``.

    The ``sequence_no`` field carries the monotonic-across-types counter
    used by the api's replay buffer; the subscriber uses it as the
    ``Last-Event-ID`` value on reconnect.
    """

    event_type: str
    sequence_no: int
    data: dict[str, Any]
    raw_id: str


async def parse_sse_stream(response: httpx.Response) -> AsyncIterator[SSEFrame]:
    """Parse an httpx streaming Response body as SSE frames.

    SSE wire format (per the WHATWG spec + the api's emit shape):

      id: <seq>
      event: <type>
      data: <json-string>
      <blank line>

    The api's emit shape is one envelope per frame, with the `data:`
    line carrying the full JCS-canonical envelope including
    ``{type, sequence_no, server_now, data}``. We yield ``SSEFrame``
    using the *envelope*'s ``type`` + ``sequence_no`` + ``data`` fields
    (NOT the SSE-protocol headers) because the envelope is the
    authoritative source — the wire ``event:`` line happens to mirror
    it, but the envelope is the contract.

    Lines starting with ``:`` are SSE comments (heartbeats); skipped.
    Unparseable frames (no ``data:`` line, malformed JSON) are logged
    and dropped — never raised — so a single bad frame doesn't crash
    the subscriber loop.
    """
    current_id: str = ""
    current_event: str = ""
    data_lines: list[str] = []

    async for raw_line in response.aiter_lines():
        # httpx.aiter_lines() yields lines WITHOUT trailing newlines.
        # Blank line = end-of-frame.
        if raw_line == "":
            frame = _finalize_frame(current_id, current_event, data_lines)
            if frame is not None:
                yield frame
            current_id = ""
            current_event = ""
            data_lines = []
            continue

        # SSE comments start with `:`. The api may emit these for
        # keepalive; ignore.
        if raw_line.startswith(":"):
            continue

        # Lines without a colon are syntactically invalid per the SSE
        # spec; tolerate by ignoring.
        if ":" not in raw_line:
            continue

        field, _, value = raw_line.partition(":")
        # The SSE spec says exactly one space after the colon is
        # consumed; we strip leading whitespace defensively.
        value = value.lstrip(" ")
        if field == "id":
            current_id = value
        elif field == "event":
            current_event = value
        elif field == "data":
            data_lines.append(value)
        # `retry` and other fields ignored.


def _finalize_frame(raw_id: str, raw_event: str, data_lines: list[str]) -> SSEFrame | None:
    """Assemble accumulated SSE lines into a :class:`SSEFrame`.

    Drops the frame on malformed JSON or missing envelope fields. Logs
    a warning with the offending line snippet so operators can debug
    without the loop crashing.
    """
    if not data_lines:
        return None
    # The api's emit shape is ONE `data:` line per frame (single JCS-
    # canonical envelope). Multi-line `data:` (per the SSE spec, joined
    # with literal newlines) is tolerated for forward compatibility but
    # not produced today.
    data_blob = "\n".join(data_lines)
    try:
        envelope = json.loads(data_blob)
    except json.JSONDecodeError as exc:
        log.warning(
            "sse_frame_malformed_json",
            raw_id=raw_id,
            error=str(exc),
            snippet=data_blob[:120],
        )
        return None

    if not isinstance(envelope, dict):
        log.warning("sse_frame_envelope_not_dict", raw_id=raw_id)
        return None

    env_type = envelope.get("type")
    if not isinstance(env_type, str):
        log.warning("sse_frame_missing_type", raw_id=raw_id)
        return None
    seq_no = envelope.get("sequence_no")
    if not isinstance(seq_no, int):
        log.warning("sse_frame_missing_sequence_no", raw_id=raw_id)
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        log.warning("sse_frame_data_not_dict", raw_id=raw_id)
        return None

    return SSEFrame(
        event_type=env_type,
        sequence_no=seq_no,
        data=data,
        raw_id=raw_id or str(seq_no),
    )


# ---------------------------------------------------------------------------
# Event router
# ---------------------------------------------------------------------------


def route_event(frame: SSEFrame) -> list[tuple[EventChannel, EventPushPlan]]:
    """Map an :class:`SSEFrame` to zero or more event-push plans.

    Returns a list (instead of an Optional) because one SSE event might
    in the future fan out to multiple Discord embeds (e.g., a fill that
    closes a position could push both ``#fills`` and ``#positions``).
    Today every event produces 0 or 1 plans.

    Unrecognized event types (or expected types with missing payload
    fields) are logged + return an empty list. Never raises.
    """
    et = frame.event_type
    data = frame.data

    if et == "signal":
        return _route_signal_event(data, frame.sequence_no)
    if et == "fill":
        return _route_fill_event(data, frame.sequence_no)
    # All other types ignored.
    return []


def _route_signal_event(
    data: dict[str, Any], sequence_no: int
) -> list[tuple[EventChannel, EventPushPlan]]:
    action = str(data.get("action", "")).lower()
    market = str(data.get("market", ""))
    direction = str(data.get("direction", ""))
    signal_id = str(data.get("signal_id", ""))
    environment = str(data.get("environment", "paper"))

    if not signal_id:
        log.warning("sse_signal_missing_signal_id", sequence_no=sequence_no, action=action)
        return []
    if not market or not direction:
        log.warning(
            "sse_signal_missing_market_or_direction",
            sequence_no=sequence_no,
            action=action,
            signal_id=signal_id,
        )
        return []

    if action == "emitted":
        try:
            event = SignalEmittedEvent(
                signal_id=signal_id,
                market=market,
                direction=direction,
                target_contracts=int(data.get("target_contracts", 0)),
                decision_price=_to_decimal(data.get("decision_price")),
                emitted_at_utc=_to_utc_datetime(data.get("emitted_at_utc")),
                environment=environment,
                strategy_version=str(data.get("strategy_version", "unknown")),
                anomaly_reasons=tuple(data.get("anomaly_reasons") or ()),
            )
        except (ValueError, InvalidOperation, TypeError) as exc:
            log.warning(
                "sse_signal_emitted_invalid_payload",
                sequence_no=sequence_no,
                error=str(exc),
            )
            return []
        plan = plan_event_push(channel=EventChannel.SIGNALS, event=event)
        return [(EventChannel.SIGNALS, plan)]

    if action in ("approved", "rejected", "deferred"):
        try:
            decision_event = SignalDecisionEvent(
                signal_id=signal_id,
                market=market,
                direction=direction,
                decision=action,
                decided_at_utc=_to_utc_datetime(data.get("decided_at_utc")),
                decided_by_user_id=str(data.get("decided_by_user_id", "unknown")),
                environment=environment,
                diary_reasoning_text=(
                    str(data["diary_reasoning_text"]) if data.get("diary_reasoning_text") else None
                ),
            )
        except (ValueError, InvalidOperation, TypeError) as exc:
            log.warning(
                "sse_signal_decision_invalid_payload",
                sequence_no=sequence_no,
                error=str(exc),
                action=action,
            )
            return []
        plan = plan_event_push(channel=EventChannel.SIGNALS, event=decision_event)
        return [(EventChannel.SIGNALS, plan)]

    if action == "placed":
        try:
            placed_event = OrderPlacedEvent(
                signal_id=signal_id,
                market=market,
                direction=direction,
                side=str(data.get("side", "")).lower(),
                quantity=str(data.get("quantity", "")),
                order_id=str(data.get("order_id", "")),
                client_order_id=str(data.get("client_order_id", "")),
                broker_order_id=(
                    str(data["broker_order_id"]) if data.get("broker_order_id") else None
                ),
                broker_status=str(data.get("broker_status", "unknown")),
                db_status=str(data.get("db_status", "unknown")),
                placed_at_utc=_to_utc_datetime(data.get("placed_at_utc")),
                environment=environment,
                rejection_detail=(
                    str(data["rejection_detail"]) if data.get("rejection_detail") else None
                ),
            )
        except (ValueError, InvalidOperation, TypeError) as exc:
            log.warning(
                "sse_signal_placed_invalid_payload",
                sequence_no=sequence_no,
                error=str(exc),
            )
            return []
        if not placed_event.order_id or not placed_event.client_order_id:
            log.warning(
                "sse_signal_placed_missing_order_id",
                sequence_no=sequence_no,
                signal_id=signal_id,
            )
            return []
        plan = plan_event_push(channel=EventChannel.FILLS, event=placed_event)
        return [(EventChannel.FILLS, plan)]

    # Unknown action; ignore silently.
    return []


def _route_fill_event(
    data: dict[str, Any], sequence_no: int
) -> list[tuple[EventChannel, EventPushPlan]]:
    """Route a ``fill`` SSE envelope to an ``order_filled`` Discord embed.

    The data shape expected here mirrors the eventual emit_sse call site
    in the order placement / fill ingestion path. Missing fields cause
    the frame to be dropped (logged) rather than dispatched with a
    partial embed.
    """
    order_id = str(data.get("order_id") or data.get("broker_order_id") or "")
    client_order_id = str(data.get("client_order_id", ""))
    signal_id = str(data.get("signal_id", ""))
    market = str(data.get("market", ""))
    side = str(data.get("side", "")).lower()
    environment = str(data.get("environment", "paper"))

    if not order_id or not client_order_id or not signal_id or not market or not side:
        log.warning(
            "sse_fill_missing_required_fields",
            sequence_no=sequence_no,
            order_id=order_id,
            client_order_id=client_order_id,
            signal_id=signal_id,
            market=market,
            side=side,
        )
        return []

    try:
        event = OrderFilledEvent(
            order_id=order_id,
            client_order_id=client_order_id,
            signal_id=signal_id,
            market=market,
            side=side,
            quantity=_to_decimal(data.get("quantity")),
            fill_price=_to_decimal(data.get("fill_price")),
            commission_usd=_to_decimal(data.get("commission_usd", "0")),
            filled_at_utc=_to_utc_datetime(data.get("filled_at_utc")),
            environment=environment,
        )
    except (ValueError, InvalidOperation, TypeError) as exc:
        log.warning(
            "sse_fill_invalid_payload",
            sequence_no=sequence_no,
            error=str(exc),
        )
        return []
    plan = plan_event_push(channel=EventChannel.FILLS, event=event)
    return [(EventChannel.FILLS, plan)]


def _to_decimal(value: Any) -> Decimal:
    """Parse an SSE-data value as Decimal.

    The api emits Decimals as strings per dev-guide §3.8; this helper
    handles the str / int / Decimal cases uniformly and raises on
    floats (A05) or malformed strings.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # A05: never accept float for money. The api shouldn't emit
        # floats, but if a non-spec producer does, reject loudly.
        raise ValueError(f"refusing to parse float-typed Decimal value: {value!r}")
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError(f"cannot parse Decimal from {type(value).__name__}: {value!r}")


def _to_utc_datetime(value: Any) -> datetime:
    """Parse an SSE-data value as tz-aware UTC datetime.

    The api emits RFC 3339 strings with millisecond precision + `Z` suffix
    (e.g., ``2026-05-12T18:00:00.123Z``). We tolerate both `Z` and the
    explicit `+00:00` offset forms.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime not permitted (A06)")
        return value.astimezone(UTC)
    if not isinstance(value, str):
        raise ValueError(f"cannot parse datetime from {type(value).__name__}: {value!r}")
    # Python's fromisoformat accepts `+00:00` natively; pre-3.11 needs
    # the `Z` → `+00:00` substitution. We're on 3.11+ but the swap is
    # harmless either way.
    iso = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        raise ValueError(f"datetime string lacks timezone offset: {value!r}")
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Long-lived subscriber loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SSESubscriberConfig:
    """Runtime configuration for :class:`SSESubscriber`.

    Sourced from the webhook_pusher container's env vars. Defaults are
    sane for the Docker-compose deploy on the operator's VPS.
    """

    api_base_url: str  # e.g., "http://api:8000"
    api_bearer_token: str  # bearer for /api/sse/events; reuses discord-bot bearer
    signals_webhook_url: str
    fills_webhook_url: str
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    request_timeout_seconds: float = 600.0


class SSESubscriber:
    """Long-lived consumer of the api's SSE stream.

    Wires the frame parser + event router + dispatcher together,
    handles reconnect-with-Last-Event-ID per backend-spec §4.2.3, and
    surfaces structured logging for each connect / disconnect / route.

    Lifecycle:
        worker = SSESubscriber(config)
        await worker.run_forever()  # blocks until stop_event.set()
        worker.request_stop()       # graceful shutdown signal

    The constructor takes a config dataclass to keep the network knobs
    + URL resolution out of the class body. Tests pass a config with
    a respx-mocked api URL + fake webhook URLs.
    """

    def __init__(self, config: SSESubscriberConfig) -> None:
        self._config = config
        self._stop_event = asyncio.Event()
        self._last_event_id: int = 0
        self._log = log.bind(
            subscriber="sse",
            api_base_url=config.api_base_url,
        )

    def request_stop(self) -> None:
        """Signal :meth:`run_forever` to exit at the next iteration boundary."""
        self._stop_event.set()

    async def run_forever(self) -> None:
        """Connect, consume, reconnect-on-failure until request_stop().

        Reconnect backoff is exponential between
        ``reconnect_initial_seconds`` and ``reconnect_max_seconds``; on
        successful frame, backoff resets to the initial value so a
        flapping connection doesn't push the cadence to the cap.
        """
        backoff = self._config.reconnect_initial_seconds
        self._log.info("sse_subscriber_started")
        try:
            while not self._stop_event.is_set():
                try:
                    consumed = await self._run_one_connection()
                    if consumed > 0:
                        backoff = self._config.reconnect_initial_seconds
                except Exception as exc:
                    self._log.warning(
                        "sse_subscriber_connection_error",
                        error=str(exc),
                        backoff_seconds=backoff,
                    )
                # Sleep with stop-aware wait so request_stop() exits
                # promptly.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2.0, self._config.reconnect_max_seconds)
        finally:
            self._log.info("sse_subscriber_stopped")

    async def _run_one_connection(self) -> int:
        """Single connect-and-consume pass; returns the number of frames consumed."""
        headers = {
            "Authorization": f"Bearer {self._config.api_bearer_token}",
            "User-Agent": "trading-webhook-pusher-sse/0.1",
            "Accept": "text/event-stream",
        }
        if self._last_event_id > 0:
            headers["Last-Event-ID"] = str(self._last_event_id)

        url = f"{self._config.api_base_url.rstrip('/')}/api/sse/events"
        consumed = 0
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                self._config.request_timeout_seconds,
                connect=10.0,
                read=None,  # no read timeout — SSE is long-lived
            )
        ) as client:
            self._log.info(
                "sse_subscriber_connecting",
                url=url,
                last_event_id=self._last_event_id,
            )
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 426:
                    # Replay buffer expired; reset Last-Event-ID to fresh
                    # connect on the next iteration. The api's 426 body
                    # carries a `version` envelope per spec §4.2.3.
                    self._log.warning(
                        "sse_subscriber_buffer_expired_reset",
                        previous_last_event_id=self._last_event_id,
                    )
                    self._last_event_id = 0
                    return 0
                if response.status_code != 200:
                    body = await response.aread()
                    self._log.warning(
                        "sse_subscriber_bad_status",
                        http_status=response.status_code,
                        snippet=body[:120].decode(errors="replace"),
                    )
                    return 0

                self._log.info("sse_subscriber_connected", http_status=200)
                async for frame in parse_sse_stream(response):
                    consumed += 1
                    self._last_event_id = max(self._last_event_id, frame.sequence_no)
                    await self._handle_frame(client, frame)
                    if self._stop_event.is_set():
                        break
        return consumed

    async def _handle_frame(self, client: httpx.AsyncClient, frame: SSEFrame) -> None:
        """Route + dispatch a single frame; never raises."""
        plans = route_event(frame)
        if not plans:
            return
        for channel, plan in plans:
            webhook_url = self._webhook_url_for(channel)
            if not webhook_url:
                self._log.warning(
                    "sse_subscriber_missing_webhook_url",
                    channel=channel.value,
                    dedupe_key=plan.dedupe_key,
                )
                continue
            try:
                result = await dispatch_event_push(
                    plan,
                    http_client=client,
                    webhook_url=webhook_url,
                )
            except Exception as exc:
                self._log.warning(
                    "sse_subscriber_dispatch_error",
                    channel=channel.value,
                    dedupe_key=plan.dedupe_key,
                    error=str(exc),
                )
                continue
            self._log.info(
                "sse_subscriber_dispatched",
                channel=channel.value,
                dedupe_key=plan.dedupe_key,
                delivery_status=result.status.value,
                http_status=result.http_status,
            )

    def _webhook_url_for(self, channel: EventChannel) -> str:
        if channel == EventChannel.SIGNALS:
            return self._config.signals_webhook_url
        if channel == EventChannel.FILLS:
            return self._config.fills_webhook_url
        return ""


__all__ = [
    "SSEFrame",
    "SSESubscriber",
    "SSESubscriberConfig",
    "parse_sse_stream",
    "route_event",
]
