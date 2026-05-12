"""Unit tests for :func:`services.webhook_pusher.dispatcher.dispatch_event_push`.

Worker-PR-2 (post-pivot 2026-05-12). respx-mocked HTTP; pure async tests.
A22 N/A (no audit_log writes; no DB).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from services.webhook_pusher.dispatcher import (
    _classify_http_status,
    dispatch_event_push,
)
from services.webhook_pusher.event_pushes import (
    EventChannel,
    EventPushPlan,
    OrderFilledEvent,
    SignalEmittedEvent,
    plan_event_push,
)
from services.webhook_pusher.sender import DeliveryStatus

_DISCORD_WEBHOOK = "https://discord.com/api/webhooks/123/abc"


def _signal_emitted_plan() -> EventPushPlan:
    """Build a representative EventPushPlan for signal_emitted."""
    event = SignalEmittedEvent(
        signal_id="0190f810-91d5-7000-8000-000000000001",
        market="/MES",
        direction="long",
        target_contracts=2,
        decision_price=Decimal("4250.50"),
        emitted_at_utc=datetime(2026, 5, 12, 21, 30, tzinfo=UTC),
        environment="paper",
        strategy_version="v1abcde",
    )
    return plan_event_push(channel=EventChannel.SIGNALS, event=event)


def _order_filled_plan() -> EventPushPlan:
    """Build a representative EventPushPlan for order_filled."""
    event = OrderFilledEvent(
        order_id="98765",
        client_order_id="abc12345-def67890-12345678-0",
        signal_id="0190f810-91d5-7000-8000-000000000002",
        market="/MES",
        side="buy",
        quantity=Decimal("2"),
        fill_price=Decimal("4250.75"),
        commission_usd=Decimal("0.85"),
        filled_at_utc=datetime(2026, 5, 12, 21, 30, 5, tzinfo=UTC),
        environment="paper",
    )
    return plan_event_push(channel=EventChannel.FILLS, event=event)


class TestDispatchEventPushHappyPath:
    """Discord webhook returns 204 → DeliveryStatus.OK."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_signal_emitted_204_ok(self) -> None:
        route = respx.post(_DISCORD_WEBHOOK).respond(204)
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert route.called
        assert result.status is DeliveryStatus.OK
        assert result.http_status == 204
        assert result.error is None
        assert result.retried is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_order_filled_204_ok(self) -> None:
        route = respx.post(_DISCORD_WEBHOOK).respond(204)
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _order_filled_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert route.called
        assert result.status is DeliveryStatus.OK

    @pytest.mark.asyncio
    @respx.mock
    async def test_post_body_includes_embed(self) -> None:
        """Verify the request body is the canonical Discord embed shape."""
        captured: list[bytes] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            captured.append(request.content)
            return httpx.Response(204)

        respx.post(_DISCORD_WEBHOOK).mock(side_effect=_capture)
        async with httpx.AsyncClient() as client:
            await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert len(captured) == 1
        # Body shape: {"embeds": [...]} per Discord webhook spec.
        body = captured[0].decode("utf-8")
        assert '"embeds"' in body
        assert "/MES" in body  # the market field surfaces in the embed

    @pytest.mark.asyncio
    @respx.mock
    async def test_user_agent_header_set(self) -> None:
        """The webhook_pusher User-Agent identifies our caller."""
        captured: list[str] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("User-Agent", ""))
            return httpx.Response(204)

        respx.post(_DISCORD_WEBHOOK).mock(side_effect=_capture)
        async with httpx.AsyncClient() as client:
            await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert captured[0].startswith("trading-webhook-pusher/")


class TestDispatchEventPushFailures:
    @pytest.mark.asyncio
    @respx.mock
    async def test_401_is_failed_auth(self) -> None:
        respx.post(_DISCORD_WEBHOOK).respond(401, text="unauthorized")
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert result.status is DeliveryStatus.FAILED_AUTH

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_is_failed_not_found(self) -> None:
        respx.post(_DISCORD_WEBHOOK).respond(404)
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert result.status is DeliveryStatus.FAILED_NOT_FOUND

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_is_rate_limited(self) -> None:
        respx.post(_DISCORD_WEBHOOK).respond(429, json={"retry_after": 1.0})
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert result.status is DeliveryStatus.RATE_LIMITED

    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_is_server_error(self) -> None:
        respx.post(_DISCORD_WEBHOOK).respond(503)
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert result.status is DeliveryStatus.FAILED_SERVER_ERROR

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout(self) -> None:
        respx.post(_DISCORD_WEBHOOK).mock(side_effect=httpx.TimeoutException("timeout"))
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert result.status is DeliveryStatus.FAILED_TIMEOUT

    @pytest.mark.asyncio
    @respx.mock
    async def test_network_error(self) -> None:
        respx.post(_DISCORD_WEBHOOK).mock(side_effect=httpx.ConnectError("conn refused"))
        async with httpx.AsyncClient() as client:
            result = await dispatch_event_push(
                _signal_emitted_plan(),
                http_client=client,
                webhook_url=_DISCORD_WEBHOOK,
            )
        assert result.status is DeliveryStatus.FAILED_NETWORK


class TestClassifyHttpStatus:
    """Verify the boundary cases of HTTP-status → DeliveryStatus."""

    @pytest.mark.parametrize(
        "http_status,expected",
        [
            (200, DeliveryStatus.OK),
            (201, DeliveryStatus.OK),
            (204, DeliveryStatus.OK),
            (299, DeliveryStatus.OK),
            (401, DeliveryStatus.FAILED_AUTH),
            (403, DeliveryStatus.FAILED_AUTH),
            (404, DeliveryStatus.FAILED_NOT_FOUND),
            (400, DeliveryStatus.FAILED_CLIENT_ERROR),
            (422, DeliveryStatus.FAILED_CLIENT_ERROR),
            (429, DeliveryStatus.RATE_LIMITED),
            (500, DeliveryStatus.FAILED_SERVER_ERROR),
            (503, DeliveryStatus.FAILED_SERVER_ERROR),
            (599, DeliveryStatus.FAILED_SERVER_ERROR),
            (100, DeliveryStatus.FAILED_NETWORK),
            (600, DeliveryStatus.FAILED_NETWORK),
        ],
    )
    def test_status_classification(self, http_status: int, expected: DeliveryStatus) -> None:
        assert _classify_http_status(http_status) == expected
