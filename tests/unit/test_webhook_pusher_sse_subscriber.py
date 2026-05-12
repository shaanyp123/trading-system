"""Unit tests for ``services.webhook_pusher.sse_subscriber``.

Worker-PR-2 follow-up (post-pivot 2026-05-12). Tests three independently-
verifiable surfaces:

  * Frame parsing (``parse_sse_stream``) — SSE wire shape → SSEFrame.
  * Event routing (``route_event``) — SSEFrame → event-push plans.
  * Subscriber lifecycle (``SSESubscriber``) — connect / consume /
    reconnect with respx-mocked HTTP.

A22 N/A — pure async + respx-mocked HTTP, no Postgres.
A06 enforced — every datetime tz-aware UTC.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from services.webhook_pusher.event_pushes import EventChannel
from services.webhook_pusher.sse_subscriber import (
    SSEFrame,
    SSESubscriber,
    SSESubscriberConfig,
    _to_decimal,
    _to_utc_datetime,
    parse_sse_stream,
    route_event,
)

# ---------------------------------------------------------------------------
# Frame parser
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Stand-in for an httpx.Response that yields a canned line sequence."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


def _build_envelope(
    *,
    event_type: str = "signal",
    sequence_no: int = 1,
    data: dict[str, Any] | None = None,
) -> str:
    envelope = {
        "type": event_type,
        "sequence_no": sequence_no,
        "server_now": "2026-05-12T18:00:00.000Z",
        "data": data or {},
    }
    return json.dumps(envelope)


class TestParseSSEStream:
    @pytest.mark.asyncio
    async def test_single_frame_parsed(self) -> None:
        payload = _build_envelope(
            event_type="signal",
            sequence_no=42,
            data={"action": "emitted", "signal_id": "abc"},
        )
        lines = [
            "id: 42",
            "event: signal",
            f"data: {payload}",
            "",
        ]
        stream = _FakeStreamResponse(lines)
        frames = [f async for f in parse_sse_stream(stream)]  # type: ignore[arg-type]
        assert len(frames) == 1
        assert frames[0].event_type == "signal"
        assert frames[0].sequence_no == 42
        assert frames[0].data == {"action": "emitted", "signal_id": "abc"}

    @pytest.mark.asyncio
    async def test_two_frames_separated_by_blank_line(self) -> None:
        p1 = _build_envelope(event_type="signal", sequence_no=1)
        p2 = _build_envelope(event_type="fill", sequence_no=2)
        lines = [
            "id: 1",
            f"data: {p1}",
            "",
            "id: 2",
            f"data: {p2}",
            "",
        ]
        stream = _FakeStreamResponse(lines)
        frames = [f async for f in parse_sse_stream(stream)]  # type: ignore[arg-type]
        assert [f.sequence_no for f in frames] == [1, 2]
        assert [f.event_type for f in frames] == ["signal", "fill"]

    @pytest.mark.asyncio
    async def test_comment_lines_skipped(self) -> None:
        payload = _build_envelope(sequence_no=7)
        lines = [
            ": keepalive comment",
            f"data: {payload}",
            "",
        ]
        stream = _FakeStreamResponse(lines)
        frames = [f async for f in parse_sse_stream(stream)]  # type: ignore[arg-type]
        assert len(frames) == 1
        assert frames[0].sequence_no == 7

    @pytest.mark.asyncio
    async def test_malformed_json_dropped(self) -> None:
        lines = [
            "data: not-json",
            "",
        ]
        stream = _FakeStreamResponse(lines)
        frames = [f async for f in parse_sse_stream(stream)]  # type: ignore[arg-type]
        assert frames == []

    @pytest.mark.asyncio
    async def test_missing_type_dropped(self) -> None:
        payload = json.dumps({"sequence_no": 1, "data": {}})
        lines = [f"data: {payload}", ""]
        stream = _FakeStreamResponse(lines)
        frames = [f async for f in parse_sse_stream(stream)]  # type: ignore[arg-type]
        assert frames == []

    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        stream = _FakeStreamResponse([])
        frames = [f async for f in parse_sse_stream(stream)]  # type: ignore[arg-type]
        assert frames == []

    @pytest.mark.asyncio
    async def test_data_dropped_if_not_dict(self) -> None:
        envelope = json.dumps({"type": "signal", "sequence_no": 1, "data": "string-not-dict"})
        lines = [f"data: {envelope}", ""]
        stream = _FakeStreamResponse(lines)
        frames = [f async for f in parse_sse_stream(stream)]  # type: ignore[arg-type]
        assert frames == []


# ---------------------------------------------------------------------------
# Event router
# ---------------------------------------------------------------------------


def _signal_emitted_data(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "emitted",
        "signal_id": "11111111-1111-1111-1111-111111111111",
        "market": "/MES",
        "direction": "long",
        "target_contracts": 1,
        "decision_price": "5250.00",
        "emitted_at_utc": "2026-05-12T18:00:00Z",
        "environment": "paper",
        "strategy_version": "v1abc12",
    }
    base.update(overrides)
    return base


def _signal_placed_data(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "placed",
        "signal_id": "55555555-5555-5555-5555-555555555555",
        "market": "/MES",
        "direction": "long",
        "side": "buy",
        "quantity": "1",
        "order_id": "66666666-6666-6666-6666-666666666666",
        "client_order_id": "v1abc-paramx-sigy-0",
        "broker_order_id": "12345",
        "broker_status": "submitted",
        "db_status": "working",
        "placed_at_utc": "2026-05-12T18:06:00Z",
        "environment": "paper",
    }
    base.update(overrides)
    return base


def _signal_decision_data(action: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": action,
        "signal_id": "22222222-2222-2222-2222-222222222222",
        "market": "/MES",
        "direction": "long",
        "decided_at_utc": "2026-05-12T18:05:00Z",
        "decided_by_user_id": "operator",
        "environment": "paper",
    }
    base.update(overrides)
    return base


def _fill_data(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_id": "33333333",
        "client_order_id": "v1abc-paramx-sigy-0",
        "signal_id": "44444444-4444-4444-4444-444444444444",
        "market": "/MES",
        "side": "buy",
        "quantity": "1",
        "fill_price": "5249.75",
        "commission_usd": "0.85",
        "filled_at_utc": "2026-05-12T18:10:00Z",
        "environment": "paper",
    }
    base.update(overrides)
    return base


class TestRouteEvent:
    def test_signal_emitted_routes_to_signals_channel(self) -> None:
        frame = SSEFrame(
            event_type="signal",
            sequence_no=1,
            data=_signal_emitted_data(),
            raw_id="1",
        )
        plans = route_event(frame)
        assert len(plans) == 1
        channel, plan = plans[0]
        assert channel == EventChannel.SIGNALS
        assert plan.dedupe_key.startswith("signal_emitted:")

    def test_signal_approved_routes_to_signals_channel(self) -> None:
        frame = SSEFrame(
            event_type="signal",
            sequence_no=2,
            data=_signal_decision_data("approved"),
            raw_id="2",
        )
        plans = route_event(frame)
        assert len(plans) == 1
        channel, plan = plans[0]
        assert channel == EventChannel.SIGNALS
        assert plan.dedupe_key.startswith("signal_approved:")

    def test_signal_rejected_routes_to_signals_channel(self) -> None:
        frame = SSEFrame(
            event_type="signal",
            sequence_no=3,
            data=_signal_decision_data("rejected"),
            raw_id="3",
        )
        plans = route_event(frame)
        assert len(plans) == 1
        assert plans[0][0] == EventChannel.SIGNALS

    def test_signal_deferred_routes_to_signals_channel(self) -> None:
        frame = SSEFrame(
            event_type="signal",
            sequence_no=4,
            data=_signal_decision_data("deferred"),
            raw_id="4",
        )
        plans = route_event(frame)
        assert len(plans) == 1
        assert plans[0][0] == EventChannel.SIGNALS

    def test_signal_unknown_action_ignored(self) -> None:
        frame = SSEFrame(
            event_type="signal",
            sequence_no=5,
            data=_signal_decision_data("never-heard-of-this"),
            raw_id="5",
        )
        plans = route_event(frame)
        assert plans == []

    def test_signal_missing_signal_id_dropped(self) -> None:
        frame = SSEFrame(
            event_type="signal",
            sequence_no=6,
            data=_signal_emitted_data(signal_id=""),
            raw_id="6",
        )
        plans = route_event(frame)
        assert plans == []

    def test_signal_placed_routes_to_fills_channel(self) -> None:
        """`signal` event with action=placed → #fills channel + order_placed dedupe key."""
        frame = SSEFrame(
            event_type="signal",
            sequence_no=20,
            data=_signal_placed_data(),
            raw_id="20",
        )
        plans = route_event(frame)
        assert len(plans) == 1
        channel, plan = plans[0]
        assert channel == EventChannel.FILLS
        assert plan.dedupe_key.startswith("order_placed:")

    def test_signal_placed_missing_order_id_dropped(self) -> None:
        frame = SSEFrame(
            event_type="signal",
            sequence_no=21,
            data=_signal_placed_data(order_id="", client_order_id=""),
            raw_id="21",
        )
        plans = route_event(frame)
        assert plans == []

    def test_signal_placed_rejected_status_renders_red_embed(self) -> None:
        """broker_status=rejected → embed uses COLOR_ORDER_REJECTED."""
        from services.webhook_pusher.event_pushes import COLOR_ORDER_REJECTED

        frame = SSEFrame(
            event_type="signal",
            sequence_no=22,
            data=_signal_placed_data(
                broker_status="rejected",
                db_status="rejected",
                rejection_detail="insufficient margin",
            ),
            raw_id="22",
        )
        plans = route_event(frame)
        assert len(plans) == 1
        _, plan = plans[0]
        assert plan.embed["color"] == COLOR_ORDER_REJECTED

    def test_fill_routes_to_fills_channel(self) -> None:
        frame = SSEFrame(
            event_type="fill",
            sequence_no=7,
            data=_fill_data(),
            raw_id="7",
        )
        plans = route_event(frame)
        assert len(plans) == 1
        channel, plan = plans[0]
        assert channel == EventChannel.FILLS
        assert plan.dedupe_key.startswith("order_filled:")

    def test_fill_missing_order_id_dropped(self) -> None:
        frame = SSEFrame(
            event_type="fill",
            sequence_no=8,
            data=_fill_data(order_id="", client_order_id=""),
            raw_id="8",
        )
        plans = route_event(frame)
        assert plans == []

    def test_alert_event_silently_ignored(self) -> None:
        """Alerts flow through the dispatch_alert path, not the subscriber."""
        frame = SSEFrame(
            event_type="alert",
            sequence_no=9,
            data={"alert_id": "abc"},
            raw_id="9",
        )
        plans = route_event(frame)
        assert plans == []

    def test_ping_silently_ignored(self) -> None:
        frame = SSEFrame(
            event_type="ping",
            sequence_no=10,
            data={"server_now": "2026-05-12T18:00:00Z"},
            raw_id="10",
        )
        plans = route_event(frame)
        assert plans == []

    def test_position_pnl_health_risk_state_all_ignored(self) -> None:
        for et in ("position", "pnl", "health", "risk_state", "audit", "vacation"):
            frame = SSEFrame(event_type=et, sequence_no=11, data={}, raw_id="11")
            assert route_event(frame) == [], f"event_type={et} should be ignored"


# ---------------------------------------------------------------------------
# Decimal + datetime helpers
# ---------------------------------------------------------------------------


class TestToDecimal:
    def test_string_parses(self) -> None:
        assert _to_decimal("5250.00") == Decimal("5250.00")

    def test_int_parses(self) -> None:
        assert _to_decimal(1) == Decimal(1)

    def test_decimal_passthrough(self) -> None:
        assert _to_decimal(Decimal("3.14")) == Decimal("3.14")

    def test_float_rejected_per_a05(self) -> None:
        with pytest.raises(ValueError, match="float"):
            _to_decimal(3.14)

    def test_none_raises(self) -> None:
        with pytest.raises(ValueError):
            _to_decimal(None)


class TestToUtcDatetime:
    def test_z_suffix_parsed(self) -> None:
        ts = _to_utc_datetime("2026-05-12T18:00:00Z")
        assert ts == datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)

    def test_explicit_offset_parsed(self) -> None:
        ts = _to_utc_datetime("2026-05-12T18:00:00+00:00")
        assert ts == datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)

    def test_naive_string_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _to_utc_datetime("2026-05-12T18:00:00")

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            _to_utc_datetime(datetime(2026, 5, 12, 18, 0))

    def test_aware_datetime_passthrough(self) -> None:
        ts = _to_utc_datetime(datetime(2026, 5, 12, 18, 0, tzinfo=UTC))
        assert ts == datetime(2026, 5, 12, 18, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Subscriber lifecycle
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> SSESubscriberConfig:
    base = {
        "api_base_url": "http://api:8000",
        "api_bearer_token": "test-bearer",
        "signals_webhook_url": "https://discord.example.com/webhooks/signals",
        "fills_webhook_url": "https://discord.example.com/webhooks/fills",
        "reconnect_initial_seconds": 0.01,
        "reconnect_max_seconds": 0.1,
        "request_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return SSESubscriberConfig(**base)  # type: ignore[arg-type]


class TestSSESubscriber:
    @pytest.mark.asyncio
    @respx.mock
    async def test_consumes_one_frame_and_dispatches(self) -> None:
        """Subscriber connects, parses a signal_emitted frame, and POSTs to #signals."""
        payload = _build_envelope(
            event_type="signal",
            sequence_no=1,
            data=_signal_emitted_data(),
        )
        sse_body = f"id: 1\ndata: {payload}\n\n"

        respx.get("http://api:8000/api/sse/events").mock(
            return_value=httpx.Response(
                200,
                content=sse_body,
                headers={"Content-Type": "text/event-stream"},
            )
        )
        discord_route = respx.post("https://discord.example.com/webhooks/signals").mock(
            return_value=httpx.Response(204)
        )

        subscriber = SSESubscriber(_make_config())

        # Run one connection then stop. _run_one_connection consumes the
        # whole stream then returns; we don't need run_forever for this
        # path.
        consumed = await subscriber._run_one_connection()
        assert consumed == 1
        assert subscriber._last_event_id == 1
        assert discord_route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_consumes_fill_frame_and_dispatches_to_fills_webhook(self) -> None:
        payload = _build_envelope(
            event_type="fill",
            sequence_no=5,
            data=_fill_data(),
        )
        sse_body = f"id: 5\ndata: {payload}\n\n"

        respx.get("http://api:8000/api/sse/events").mock(
            return_value=httpx.Response(
                200,
                content=sse_body,
                headers={"Content-Type": "text/event-stream"},
            )
        )
        fills_route = respx.post("https://discord.example.com/webhooks/fills").mock(
            return_value=httpx.Response(204)
        )

        subscriber = SSESubscriber(_make_config())
        consumed = await subscriber._run_one_connection()
        assert consumed == 1
        assert fills_route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_426_resets_last_event_id_to_zero(self) -> None:
        """Replay buffer expired → reset Last-Event-ID for fresh connect."""
        respx.get("http://api:8000/api/sse/events").mock(
            return_value=httpx.Response(
                426,
                content=b'{"version":"replay_buffer_expired"}',
            )
        )
        subscriber = SSESubscriber(_make_config())
        subscriber._last_event_id = 999
        consumed = await subscriber._run_one_connection()
        assert consumed == 0
        assert subscriber._last_event_id == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_status_returns_zero_consumed_no_crash(self) -> None:
        respx.get("http://api:8000/api/sse/events").mock(
            return_value=httpx.Response(500, content=b"internal error")
        )
        subscriber = SSESubscriber(_make_config())
        consumed = await subscriber._run_one_connection()
        assert consumed == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_authorization_header_attached(self) -> None:
        """The bearer is sent on every connect."""
        payload = _build_envelope(sequence_no=1, data={})
        captured: dict[str, Any] = {}

        def _hit(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("Authorization")
            captured["user_agent"] = request.headers.get("User-Agent")
            return httpx.Response(
                200,
                content=f"id: 1\ndata: {payload}\n\n",
                headers={"Content-Type": "text/event-stream"},
            )

        respx.get("http://api:8000/api/sse/events").mock(side_effect=_hit)

        subscriber = SSESubscriber(_make_config(api_bearer_token="my-bearer-xyz"))
        await subscriber._run_one_connection()
        assert captured["authorization"] == "Bearer my-bearer-xyz"
        assert "trading-webhook-pusher-sse" in (captured.get("user_agent") or "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_last_event_id_sent_on_reconnect(self) -> None:
        payload = _build_envelope(sequence_no=100, data={})
        captured: list[str | None] = []

        def _hit(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("Last-Event-ID"))
            return httpx.Response(
                200,
                content=f"id: 100\ndata: {payload}\n\n",
                headers={"Content-Type": "text/event-stream"},
            )

        respx.get("http://api:8000/api/sse/events").mock(side_effect=_hit)

        subscriber = SSESubscriber(_make_config())
        subscriber._last_event_id = 50
        await subscriber._run_one_connection()
        assert captured == ["50"]

    @pytest.mark.asyncio
    async def test_request_stop_signals_loop_exit(self) -> None:
        """request_stop() flips the stop event so run_forever() exits."""
        subscriber = SSESubscriber(_make_config())
        assert not subscriber._stop_event.is_set()
        subscriber.request_stop()
        assert subscriber._stop_event.is_set()

    @pytest.mark.asyncio
    @respx.mock
    async def test_run_forever_exits_when_stop_requested(self) -> None:
        """run_forever respects request_stop() between reconnect attempts."""
        respx.get("http://api:8000/api/sse/events").mock(
            return_value=httpx.Response(500, content=b"err")
        )
        subscriber = SSESubscriber(_make_config())
        # Schedule stop after a short delay.
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, subscriber.request_stop)
        await asyncio.wait_for(subscriber.run_forever(), timeout=2.0)
        # The test passes if run_forever returns within the timeout.
