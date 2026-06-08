"""Unit tests for ``services/webhook_pusher/`` — alert dispatch policy + sender.

Test inventory derived from the session prompt's branch list + backend-spec
§3.27 (alerts table, alert_category enum) + §1.6 + §2.7 (Resend backup
contract):

Pure-policy planner (``services/webhook_pusher/payloads.py``):

- TestSeverityRouting — P0 → {alerts, critical, email}; P1/P2 → {alerts}
- TestDiscordPayloadShape — color matches severity; title is category enum
  value; footer carries fired_at_utc + alert id
- TestResendPayloadShape — subject prefix matches severity; body includes
  detail JSONB rendering
- TestPlannerErrors — missing webhook URL → WebhookPusherError; missing
  email_identity when EMAIL routed → WebhookPusherError; tz-naive
  fired_at_utc → WebhookPusherError (A06)
- TestPlannerDeterminism — same input → same plan struct (sorted channels +
  sorted detail keys)

Sender (``services/webhook_pusher/sender.py``) — respx-mocked HTTP:

- TestSenderHttpStatuses — Discord 200/204/401/404/429/5xx + Resend
  200/401/422/5xx → matching DeliveryStatus
- TestSenderRetry — 429 with Retry-After → single retry; second attempt
  outcome wins
- TestSenderTransport — timeout / connection error → FAILED_TIMEOUT /
  FAILED_NETWORK
- TestSenderHeaders — User-Agent override; Authorization header attached
  for Resend; not attached for Discord

Dispatcher (``services/webhook_pusher/dispatcher.py``):

- TestDispatcherIdempotency — pre-existing delivery_status → short-circuit
  (no httpx POST, no UPDATE)

A22 binds: zero live HTTP, zero audit writes, zero testcontainers. All
HTTP via respx; the dispatcher idempotency test uses a fake AsyncSession
+ a fake AsyncClient.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from services.webhook_pusher.dispatcher import (
    AlertNotFoundError,
    DeliveryReport,
    dispatch_alert,
)
from services.webhook_pusher.payloads import (
    DISCORD_FIELD_VALUE_MAX,
    EMAIL_SUBJECT_MESSAGE_MAX,
    RESEND_API_URL,
    SEVERITY_TO_CHANNELS,
    SEVERITY_TO_DISCORD_COLOR,
    AlertCategory,
    AlertDispatchPlan,
    AlertRow,
    AlertSeverity,
    ChannelName,
    EmailIdentity,
    OutboundMessage,
    WebhookPusherError,
    plan_alert_dispatch,
)
from services.webhook_pusher.sender import (
    WEBHOOK_PUSHER_USER_AGENT,
    DeliveryResult,
    DeliveryStatus,
    post_outbound_message,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


_FIXED_FIRED_AT = datetime(2026, 5, 11, 13, 30, 0, tzinfo=UTC)
_FIXED_ALERT_ID = UUID("0193abc4-0000-7321-89ab-cdef01234567")

_DISCORD_ALERTS_URL = "https://discord.test/api/webhooks/100/alerts-token"
_DISCORD_CRITICAL_URL = "https://discord.test/api/webhooks/200/critical-token"


def _alert(
    *,
    severity: AlertSeverity = AlertSeverity.P2,
    category: AlertCategory = AlertCategory.EXTERNAL_WATCHDOG_ALERT,
    message: str = "smoke test",
    detail: Mapping[str, Any] | None = None,
    fired_at_utc: datetime = _FIXED_FIRED_AT,
    alert_id: UUID = _FIXED_ALERT_ID,
) -> AlertRow:
    return AlertRow(
        id=alert_id,
        severity=severity,
        category=category,
        message=message,
        detail=detail,
        fired_at_utc=fired_at_utc,
    )


def _identity() -> EmailIdentity:
    return EmailIdentity(
        from_address="alerts@spratcapital.test",
        to_address="operator@spratcapital.test",
        resend_api_key="re_test_xxx",
    )


def _all_webhook_urls() -> dict[ChannelName, str]:
    return {
        ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL,
        ChannelName.DISCORD_CRITICAL: _DISCORD_CRITICAL_URL,
        ChannelName.EMAIL: RESEND_API_URL,
    }


# ---------------------------------------------------------------------------
# Locked constants — sanity checks that codify what the spec mandates
# ---------------------------------------------------------------------------


class TestLockedConstants:
    """If any of these change, downstream JSONB consumers break."""

    def test_severity_routing_p0_fans_out_to_three_channels(self) -> None:
        assert SEVERITY_TO_CHANNELS[AlertSeverity.P0] == frozenset(
            {
                ChannelName.DISCORD_ALERTS,
                ChannelName.DISCORD_CRITICAL,
                ChannelName.EMAIL,
            }
        )

    def test_severity_routing_p1_alerts_only(self) -> None:
        assert SEVERITY_TO_CHANNELS[AlertSeverity.P1] == frozenset({ChannelName.DISCORD_ALERTS})

    def test_severity_routing_p2_alerts_only(self) -> None:
        assert SEVERITY_TO_CHANNELS[AlertSeverity.P2] == frozenset({ChannelName.DISCORD_ALERTS})

    def test_discord_color_table(self) -> None:
        assert SEVERITY_TO_DISCORD_COLOR[AlertSeverity.P0] == 0xFF0000
        assert SEVERITY_TO_DISCORD_COLOR[AlertSeverity.P1] == 0xFFA500
        assert SEVERITY_TO_DISCORD_COLOR[AlertSeverity.P2] == 0xFFFF00

    def test_resend_api_url_locked(self) -> None:
        assert RESEND_API_URL == "https://api.resend.com/emails"


# ---------------------------------------------------------------------------
# Severity routing → channel set
# ---------------------------------------------------------------------------


class TestSeverityRouting:
    def test_p0_routes_to_alerts_critical_email(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        assert set(plan.channels) == {
            ChannelName.DISCORD_ALERTS,
            ChannelName.DISCORD_CRITICAL,
            ChannelName.EMAIL,
        }
        assert len(plan.outbound_messages) == 3

    def test_p1_routes_to_alerts_only(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P1),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        assert plan.channels == (ChannelName.DISCORD_ALERTS,)
        assert len(plan.outbound_messages) == 1
        assert plan.outbound_messages[0].channel is ChannelName.DISCORD_ALERTS

    def test_p2_routes_to_alerts_only(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P2),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        assert plan.channels == (ChannelName.DISCORD_ALERTS,)

    def test_p1_does_not_require_critical_or_email_urls(self) -> None:
        # Caller can omit URLs for unrouted channels — the planner only
        # inspects URLs for channels that severity routes to.
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P1),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
            email_identity=None,
        )
        assert ChannelName.DISCORD_CRITICAL not in plan.channels
        assert ChannelName.EMAIL not in plan.channels


# ---------------------------------------------------------------------------
# Discord embed shape
# ---------------------------------------------------------------------------


class TestDiscordPayloadShape:
    def test_p0_embed_uses_red_color(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        alerts_msg = next(
            m for m in plan.outbound_messages if m.channel is ChannelName.DISCORD_ALERTS
        )
        embed = alerts_msg.payload["embeds"][0]
        assert embed["color"] == 0xFF0000

    def test_p1_embed_uses_orange_color(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P1),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        assert embed["color"] == 0xFFA500

    def test_p2_embed_uses_yellow_color(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P2),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        assert embed["color"] == 0xFFFF00

    def test_embed_title_is_category_enum_value(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(category=AlertCategory.KILL_SWITCH_INVOKED),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        assert embed["title"] == "kill_switch_invoked"

    def test_embed_description_is_message(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(message="Reconciliation break detected on TLT"),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        assert embed["description"] == "Reconciliation break detected on TLT"

    def test_embed_footer_carries_fired_at_and_alert_id(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        footer_text = embed["footer"]["text"]
        assert _FIXED_FIRED_AT.isoformat() in footer_text
        assert str(_FIXED_ALERT_ID) in footer_text

    def test_embed_fields_from_detail_jsonb(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(detail={"market": "TLT", "delta": "5", "broker": "qc_objectstore"}),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        # Sorted alphabetically by key for determinism.
        names = [f["name"] for f in embed["fields"]]
        assert names == ["broker", "delta", "market"]
        values = {f["name"]: f["value"] for f in embed["fields"]}
        assert values["market"] == "TLT"
        assert values["delta"] == "5"
        assert values["broker"] == "qc_objectstore"

    def test_embed_field_value_truncation(self) -> None:
        long = "x" * (DISCORD_FIELD_VALUE_MAX + 100)
        plan = plan_alert_dispatch(
            alert=_alert(detail={"big": long}),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        rendered = embed["fields"][0]["value"]
        assert len(rendered) == DISCORD_FIELD_VALUE_MAX
        assert rendered.endswith("…")

    def test_embed_field_value_serializes_dict(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(detail={"nested": {"a": 1, "b": 2}}),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        # JSON-serialized with sorted keys for determinism.
        assert embed["fields"][0]["value"] == '{"a": 1, "b": 2}'

    def test_embed_field_cap_at_max_detail_fields(self) -> None:
        # 12 keys → planner caps at MAX_DETAIL_FIELDS (10).
        many = {f"k{i:02d}": str(i) for i in range(12)}
        plan = plan_alert_dispatch(
            alert=_alert(detail=many),
            webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
        )
        embed = plan.outbound_messages[0].payload["embeds"][0]
        assert len(embed["fields"]) == 10

    def test_outbound_messages_for_discord_have_no_auth_header(self) -> None:
        # Discord webhooks embed auth in the URL path; no Authorization header.
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        for msg in plan.outbound_messages:
            if msg.channel in (ChannelName.DISCORD_ALERTS, ChannelName.DISCORD_CRITICAL):
                assert msg.auth_header is None


# ---------------------------------------------------------------------------
# Resend email shape
# ---------------------------------------------------------------------------


class TestResendPayloadShape:
    def test_email_subject_includes_severity_prefix(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(
                severity=AlertSeverity.P0,
                category=AlertCategory.AUDIT_CHAIN_BREAK,
                message="hash mismatch at sequence 9281",
            ),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        email_msg = next(m for m in plan.outbound_messages if m.channel is ChannelName.EMAIL)
        subject = email_msg.payload["subject"]
        assert subject.startswith("[P0 spratcapital]")
        assert "audit_chain_break" in subject
        assert "hash mismatch at sequence 9281" in subject

    def test_email_subject_truncates_long_message(self) -> None:
        long = "x" * (EMAIL_SUBJECT_MESSAGE_MAX + 50)
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0, message=long),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        email_msg = next(m for m in plan.outbound_messages if m.channel is ChannelName.EMAIL)
        # Subject = "[P0 spratcapital] <category>: <truncated message>"
        # The category + bracket overhead pushes the total subject past
        # EMAIL_SUBJECT_MESSAGE_MAX, but the message portion is truncated.
        assert "…" in email_msg.payload["subject"]

    def test_email_body_includes_detail_rendering(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(
                severity=AlertSeverity.P0,
                detail={"market": "TLT", "delta": "5"},
            ),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        email_msg = next(m for m in plan.outbound_messages if m.channel is ChannelName.EMAIL)
        body = email_msg.payload["text"]
        assert "Detail:" in body
        assert '"market": "TLT"' in body
        assert '"delta": "5"' in body

    def test_email_to_and_from_addresses(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        email_msg = next(m for m in plan.outbound_messages if m.channel is ChannelName.EMAIL)
        assert email_msg.payload["from"] == "alerts@spratcapital.test"
        assert email_msg.payload["to"] == ["operator@spratcapital.test"]

    def test_email_outbound_message_carries_bearer_auth_header(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        email_msg = next(m for m in plan.outbound_messages if m.channel is ChannelName.EMAIL)
        assert email_msg.auth_header == "Bearer re_test_xxx"
        assert email_msg.url == RESEND_API_URL


# ---------------------------------------------------------------------------
# Planner errors
# ---------------------------------------------------------------------------


class TestPlannerErrors:
    def test_missing_webhook_url_raises(self) -> None:
        with pytest.raises(WebhookPusherError, match="discord_alerts"):
            plan_alert_dispatch(
                alert=_alert(severity=AlertSeverity.P1),
                webhook_urls={},
            )

    def test_missing_email_identity_when_p0_raises(self) -> None:
        with pytest.raises(WebhookPusherError, match="email_identity required"):
            plan_alert_dispatch(
                alert=_alert(severity=AlertSeverity.P0),
                webhook_urls=_all_webhook_urls(),
                email_identity=None,
            )

    def test_naive_datetime_raises_a06(self) -> None:
        naive = datetime(2026, 5, 11, 13, 30, 0)
        with pytest.raises(WebhookPusherError, match="A06"):
            plan_alert_dispatch(
                alert=_alert(fired_at_utc=naive),
                webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
            )


# ---------------------------------------------------------------------------
# Planner determinism
# ---------------------------------------------------------------------------


class TestPlannerDeterminism:
    def test_same_input_yields_identical_plan(self) -> None:
        urls = _all_webhook_urls()
        ident = _identity()
        a = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0, detail={"k": "v"}),
            webhook_urls=urls,
            email_identity=ident,
        )
        b = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0, detail={"k": "v"}),
            webhook_urls=urls,
            email_identity=ident,
        )
        assert a.channels == b.channels
        assert a.outbound_messages == b.outbound_messages

    def test_p0_channels_in_canonical_sorted_order(self) -> None:
        plan = plan_alert_dispatch(
            alert=_alert(severity=AlertSeverity.P0),
            webhook_urls=_all_webhook_urls(),
            email_identity=_identity(),
        )
        # Sorted by channel value: discord_alerts < discord_critical < email
        assert plan.channels == (
            ChannelName.DISCORD_ALERTS,
            ChannelName.DISCORD_CRITICAL,
            ChannelName.EMAIL,
        )


# ---------------------------------------------------------------------------
# Sender — HTTP outcome classification (respx-mocked)
# ---------------------------------------------------------------------------


def _outbound(channel: ChannelName, url: str, auth: str | None = None) -> OutboundMessage:
    return OutboundMessage(
        channel=channel,
        url=url,
        payload={"content": "smoke"},
        auth_header=auth,
    )


class TestSenderHttpStatuses:
    @pytest.mark.asyncio
    @respx.mock
    async def test_discord_204_is_ok(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(204)
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        assert result.status is DeliveryStatus.OK
        assert result.http_status == 204
        assert result.error is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_resend_200_is_ok(self) -> None:
        respx.post(RESEND_API_URL).respond(200, json={"id": "ev_abc"})
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client,
                _outbound(ChannelName.EMAIL, RESEND_API_URL, auth="Bearer re_x"),
            )
        assert result.status is DeliveryStatus.OK
        assert result.http_status == 200

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_maps_to_failed_auth(self) -> None:
        respx.post(RESEND_API_URL).respond(401, json={"name": "validation_error"})
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.EMAIL, RESEND_API_URL, auth="Bearer bad")
            )
        assert result.status is DeliveryStatus.FAILED_AUTH

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_maps_to_failed_auth(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(403, text="anti-bot block")
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        assert result.status is DeliveryStatus.FAILED_AUTH

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_maps_to_failed_not_found(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(404)
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        assert result.status is DeliveryStatus.FAILED_NOT_FOUND

    @pytest.mark.asyncio
    @respx.mock
    async def test_422_maps_to_failed_client_error(self) -> None:
        respx.post(RESEND_API_URL).respond(422, json={"message": "validation"})
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.EMAIL, RESEND_API_URL, auth="Bearer x")
            )
        assert result.status is DeliveryStatus.FAILED_CLIENT_ERROR

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_maps_to_failed_server_error(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(500, text="upstream error")
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        assert result.status is DeliveryStatus.FAILED_SERVER_ERROR

    @pytest.mark.asyncio
    @respx.mock
    async def test_503_maps_to_failed_server_error(self) -> None:
        respx.post(RESEND_API_URL).respond(503)
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.EMAIL, RESEND_API_URL, auth="Bearer x")
            )
        assert result.status is DeliveryStatus.FAILED_SERVER_ERROR


# ---------------------------------------------------------------------------
# Sender — 429 handling
# ---------------------------------------------------------------------------


class TestSenderRateLimit:
    @pytest.mark.asyncio
    @respx.mock
    async def test_429_no_retry_returns_rate_limited(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(429, headers={"Retry-After": "0.1"})
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client,
                _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL),
                retry_on_rate_limit=False,
            )
        assert result.status is DeliveryStatus.RATE_LIMITED
        assert result.retried is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_retry_then_204_succeeds(self) -> None:
        route = respx.post(_DISCORD_ALERTS_URL)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0.05"}),
            httpx.Response(204),
        ]
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        assert result.status is DeliveryStatus.OK
        assert result.retried is True
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_uses_discord_json_retry_after(self) -> None:
        # Discord prefers JSON body retry_after over the header on per-route
        # limits — the sender reads the body first.
        route = respx.post(_DISCORD_ALERTS_URL)
        route.side_effect = [
            httpx.Response(
                429,
                json={"retry_after": 0.05, "global": False},
                headers={"Retry-After": "30"},  # would-be-ignored header
            ),
            httpx.Response(204),
        ]
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        # If the sender had used the 30s header instead of the 0.05s body,
        # the retry would either time out (cap=30) or sleep too long; the
        # result.retried=True + status=OK proves it took the body path.
        assert result.retried is True
        assert result.status is DeliveryStatus.OK

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_retry_after_above_cap_does_not_retry(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(429, headers={"Retry-After": "60"})
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        # MAX_RETRY_AFTER_SECONDS = 30; 60 > 30 → no inline retry.
        assert result.status is DeliveryStatus.RATE_LIMITED
        assert result.retried is False


# ---------------------------------------------------------------------------
# Sender — transport failures
# ---------------------------------------------------------------------------


class TestSenderTransport:
    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_maps_to_failed_timeout(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).mock(side_effect=httpx.TimeoutException("timeout"))
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        assert result.status is DeliveryStatus.FAILED_TIMEOUT
        assert result.http_status is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_connect_error_maps_to_failed_network(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).mock(side_effect=httpx.ConnectError("dns failed"))
        async with httpx.AsyncClient() as client:
            result = await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        assert result.status is DeliveryStatus.FAILED_NETWORK
        assert result.http_status is None


# ---------------------------------------------------------------------------
# Sender — header attachment
# ---------------------------------------------------------------------------


class TestSenderHeaders:
    @pytest.mark.asyncio
    @respx.mock
    async def test_user_agent_overrides_httpx_default(self) -> None:
        route = respx.post(_DISCORD_ALERTS_URL).respond(204)
        async with httpx.AsyncClient() as client:
            await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        captured = route.calls.last.request
        assert captured.headers["User-Agent"] == WEBHOOK_PUSHER_USER_AGENT

    @pytest.mark.asyncio
    @respx.mock
    async def test_resend_authorization_header_attached(self) -> None:
        route = respx.post(RESEND_API_URL).respond(200, json={"id": "x"})
        async with httpx.AsyncClient() as client:
            await post_outbound_message(
                client,
                _outbound(ChannelName.EMAIL, RESEND_API_URL, auth="Bearer re_real"),
            )
        captured = route.calls.last.request
        assert captured.headers["Authorization"] == "Bearer re_real"

    @pytest.mark.asyncio
    @respx.mock
    async def test_discord_no_authorization_header(self) -> None:
        route = respx.post(_DISCORD_ALERTS_URL).respond(204)
        async with httpx.AsyncClient() as client:
            await post_outbound_message(
                client, _outbound(ChannelName.DISCORD_ALERTS, _DISCORD_ALERTS_URL)
            )
        captured = route.calls.last.request
        assert "Authorization" not in captured.headers


# ---------------------------------------------------------------------------
# Dispatcher — idempotency, not-found, fan-out wiring
# ---------------------------------------------------------------------------


class _FakeRow:
    """Minimal row mapping that mimics the SQLAlchemy mappings().one_or_none() shape."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def __getitem__(self, key: str) -> Any:
        return self._fields[key]


class _FakeMappingsResult:
    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row

    def one_or_none(self) -> _FakeRow | None:
        return self._row


class _FakeResult:
    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row

    def mappings(self) -> _FakeMappingsResult:
        return _FakeMappingsResult(self._row)


class _FakeSession:
    """Minimal AsyncSession stub for dispatcher tests.

    Captures executed statements so tests can assert on the UPDATE shape
    without spinning up a real Postgres. The dispatcher's two SQL
    statements (SELECT alerts, UPDATE alerts) are both ``text(...)`` with
    bind params; the fake replays them deterministically.
    """

    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.committed = False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self.executed.append((str(stmt), params or {}))
        sql = str(stmt).strip().upper()
        if sql.startswith("SELECT"):
            return _FakeResult(self._row)
        if sql.startswith("UPDATE"):
            return _FakeResult(None)
        raise AssertionError(f"unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.committed = True


def _row(
    *,
    severity: str = "P2",
    category: str = "external_watchdog_alert",
    delivery_status: dict[str, str] | None = None,
) -> _FakeRow:
    return _FakeRow(
        id=_FIXED_ALERT_ID,
        severity=severity,
        category=category,
        message="smoke test",
        detail={"hostname": "test"},
        fired_at_utc=_FIXED_FIRED_AT,
        delivery_status=delivery_status,
    )


class TestDispatcherIdempotency:
    @pytest.mark.asyncio
    @respx.mock
    async def test_existing_delivery_status_short_circuits(self) -> None:
        existing = {"discord_alerts": "ok"}
        session = _FakeSession(row=_row(delivery_status=existing))
        # No respx routes registered — if the dispatcher posts, it'll fail.
        async with httpx.AsyncClient() as client:
            report = await dispatch_alert(
                session=session,  # type: ignore[arg-type]
                alert_id=_FIXED_ALERT_ID,
                http_client=client,
                webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
            )
        assert report.short_circuited is True
        assert report.delivery_status == existing
        assert report.results == ()
        # Only the SELECT ran — no UPDATE.
        assert len(session.executed) == 1
        assert "SELECT" in session.executed[0][0].upper()

    @pytest.mark.asyncio
    @respx.mock
    async def test_alert_not_found_raises(self) -> None:
        session = _FakeSession(row=None)
        async with httpx.AsyncClient() as client:
            with pytest.raises(AlertNotFoundError):
                await dispatch_alert(
                    session=session,  # type: ignore[arg-type]
                    alert_id=uuid4(),
                    http_client=client,
                    webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
                )

    @pytest.mark.asyncio
    @respx.mock
    async def test_first_dispatch_posts_and_updates(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(204)
        session = _FakeSession(row=_row(severity="P2", delivery_status=None))
        async with httpx.AsyncClient() as client:
            report = await dispatch_alert(
                session=session,  # type: ignore[arg-type]
                alert_id=_FIXED_ALERT_ID,
                http_client=client,
                webhook_urls={ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL},
            )
        assert report.short_circuited is False
        assert report.delivery_status == {"discord_alerts": "ok"}
        assert len(report.results) == 1
        assert report.results[0].status is DeliveryStatus.OK
        # SELECT + UPDATE both ran.
        sqls = [s.upper().strip()[:6] for s, _ in session.executed]
        assert sqls == ["SELECT", "UPDATE"]
        assert session.committed is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_p0_with_partial_failure_records_per_channel_status(self) -> None:
        respx.post(_DISCORD_ALERTS_URL).respond(204)
        respx.post(_DISCORD_CRITICAL_URL).respond(500, text="upstream")
        respx.post(RESEND_API_URL).respond(200, json={"id": "ev"})
        session = _FakeSession(row=_row(severity="P0", delivery_status=None))
        async with httpx.AsyncClient() as client:
            report = await dispatch_alert(
                session=session,  # type: ignore[arg-type]
                alert_id=_FIXED_ALERT_ID,
                http_client=client,
                webhook_urls={
                    ChannelName.DISCORD_ALERTS: _DISCORD_ALERTS_URL,
                    ChannelName.DISCORD_CRITICAL: _DISCORD_CRITICAL_URL,
                    ChannelName.EMAIL: RESEND_API_URL,
                },
                email_identity=_identity(),
            )
        assert report.delivery_status == {
            "discord_alerts": "ok",
            "discord_critical": "failed_server_error",
            "email": "ok",
        }
        # All three results captured (one per channel).
        statuses = {r.channel: r.status for r in report.results}
        assert statuses[ChannelName.DISCORD_ALERTS] is DeliveryStatus.OK
        assert statuses[ChannelName.DISCORD_CRITICAL] is DeliveryStatus.FAILED_SERVER_ERROR
        assert statuses[ChannelName.EMAIL] is DeliveryStatus.OK


# ---------------------------------------------------------------------------
# Output-shape sanity — DeliveryStatus values are wire-canonical
# ---------------------------------------------------------------------------


class TestDeliveryStatusValues:
    """``alerts.delivery_status`` JSONB values are part of the API contract."""

    def test_canonical_status_strings(self) -> None:
        assert DeliveryStatus.OK.value == "ok"
        assert DeliveryStatus.FAILED_AUTH.value == "failed_auth"
        assert DeliveryStatus.FAILED_NOT_FOUND.value == "failed_not_found"
        assert DeliveryStatus.RATE_LIMITED.value == "rate_limited"
        assert DeliveryStatus.FAILED_CLIENT_ERROR.value == "failed_client_error"
        assert DeliveryStatus.FAILED_SERVER_ERROR.value == "failed_server_error"
        assert DeliveryStatus.FAILED_TIMEOUT.value == "failed_timeout"
        assert DeliveryStatus.FAILED_NETWORK.value == "failed_network"

    def test_channel_names_match_jsonb_keys(self) -> None:
        # The dispatcher writes channel.value as JSONB keys.
        assert ChannelName.DISCORD_ALERTS.value == "discord_alerts"
        assert ChannelName.DISCORD_CRITICAL.value == "discord_critical"
        assert ChannelName.EMAIL.value == "email"


# ---------------------------------------------------------------------------
# AlertCategory enum mirror — sanity check against the locked spec set
# ---------------------------------------------------------------------------


class TestAlertCategoryEnumMirror:
    """33 values per backend-spec §3.27 + alembic 0004 alert_category enum.

    If alembic adds a new value without updating the Python enum here,
    ``_parse_category`` raises at dispatch time. This test catches the
    drift at PR review by asserting the count + a sentinel of locked values.
    """

    def test_alert_category_count_matches_spec(self) -> None:
        # alembic 0004:311-341 lists 29 values;
        # 2026-05-17_heartbeat_stale_category migration adds 1
        # (`heartbeat_stale` for the staleness probe);
        # 2026-05-26_worker_failure_alert_category migration adds 1 more
        # (`worker_failure` for the recovery-agent task-death hook);
        # 2026-05-27_position_unprotected migration adds 1 more
        # (`position_unprotected` for the exit-pipeline PR-C
        # cancel-success+place-fail failure mode);
        # 2026-05-29_reconciliation_data_source_degraded migration adds 1
        # more (`reconciliation_data_source_degraded` for the Option C
        # recon-fix reqPositions→FlexQuery fallback push);
        # 2026-06-08_order_placement_failed migration adds 1 more
        # (`order_placement_failed` for the silent order-placement-failure
        # alert — broker unavailable / contract rejection).
        assert len(list(AlertCategory)) == 34

    def test_canonical_values_present(self) -> None:
        # Sentinel check; full set in alembic 0004 + spec §3.27.
        for v in (
            "kill_switch_invoked",
            "audit_chain_break",
            "reconciliation_break",
            "reconciliation_data_source_degraded",
            "external_watchdog_alert",
            "maintenance_window",
        ):
            assert AlertCategory(v).value == v


# ---------------------------------------------------------------------------
# Convenience: ensure DeliveryReport is importable + mappable
# ---------------------------------------------------------------------------


class TestDeliveryReport:
    def test_short_circuit_report_has_empty_results(self) -> None:
        report = DeliveryReport(
            alert_id=_FIXED_ALERT_ID,
            short_circuited=True,
            delivery_status={"discord_alerts": "ok"},
            results=(),
        )
        assert report.short_circuited is True
        assert report.results == ()


# Touch DeliveryResult constructor so the import isn't unused.
def test_delivery_result_constructible() -> None:
    r = DeliveryResult(
        channel=ChannelName.DISCORD_ALERTS,
        status=DeliveryStatus.OK,
        http_status=204,
        error=None,
    )
    assert r.channel is ChannelName.DISCORD_ALERTS


# Touch AlertDispatchPlan import for completeness — assert frozen behavior.
def test_alert_dispatch_plan_is_immutable() -> None:
    plan = AlertDispatchPlan(
        alert_id=_FIXED_ALERT_ID,
        severity=AlertSeverity.P2,
        channels=(ChannelName.DISCORD_ALERTS,),
        outbound_messages=(),
    )
    with pytest.raises(AttributeError):
        plan.alert_id = uuid4()  # type: ignore[misc]
