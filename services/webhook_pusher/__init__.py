"""services/webhook_pusher package — Discord + Resend alert delivery.

Public surface (per backend-spec §1.6 + §3.27 + §2.7 + implementation-guide
§3 Week 3 Thu):

- :func:`plan_alert_dispatch` (``payloads.py``) — pure-policy planner
  taking an :class:`AlertRow` + webhook URLs and returning an
  :class:`AlertDispatchPlan` with per-channel :class:`OutboundMessage`
  records.
- :func:`post_outbound_message` (``sender.py``) — async POST with
  identifiable User-Agent, status-code classification, single retry on
  429 with Retry-After parsing.
- :func:`dispatch_alert` (``dispatcher.py``) — orchestrator that reads
  the ``alerts`` row, calls the planner, fans out via the sender, and
  UPDATEs ``alerts.delivery_status`` JSONB. Idempotent on retry via the
  presence of an existing ``delivery_status`` value.

A22 contract: tests must NOT hit live Discord webhooks or live Resend.
The unit test suite in ``tests/unit/test_webhook_pusher.py`` uses
``respx`` to mock httpx at the transport layer; the planner has zero
HTTP at all and tests assert on plan struct data only.

A27 contract: this is a third-party platform integration. The first
commit (PR #44, Day 10 / Week 3 Thu) ships ``deploy/webhook_pusher/README.md``
as the operator-runbook smoke test per dev-guide §6.8 alternative (b).
"""

from services.webhook_pusher.dispatcher import (
    AlertNotFoundError,
    DeliveryReport,
    dispatch_alert,
)
from services.webhook_pusher.payloads import (
    DISCORD_FIELD_NAME_MAX,
    DISCORD_FIELD_VALUE_MAX,
    EMAIL_SUBJECT_MESSAGE_MAX,
    MAX_DETAIL_FIELDS,
    RESEND_API_URL,
    SEVERITY_TO_CHANNELS,
    SEVERITY_TO_DISCORD_COLOR,
    AlertCategory,
    AlertDispatchPlan,
    AlertRow,
    AlertSeverity,
    ChannelLiteral,
    ChannelName,
    EmailIdentity,
    OutboundMessage,
    WebhookPusherError,
    plan_alert_dispatch,
)
from services.webhook_pusher.sender import (
    DEFAULT_RETRY_AFTER_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    WEBHOOK_PUSHER_USER_AGENT,
    DeliveryResult,
    DeliveryStatus,
    post_outbound_message,
)

__all__ = [
    # constants
    "DEFAULT_RETRY_AFTER_SECONDS",
    "DISCORD_FIELD_NAME_MAX",
    "DISCORD_FIELD_VALUE_MAX",
    "EMAIL_SUBJECT_MESSAGE_MAX",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_DETAIL_FIELDS",
    "MAX_RETRY_AFTER_SECONDS",
    "RESEND_API_URL",
    "SEVERITY_TO_CHANNELS",
    "SEVERITY_TO_DISCORD_COLOR",
    "WEBHOOK_PUSHER_USER_AGENT",
    # enums + types
    "AlertCategory",
    "AlertDispatchPlan",
    "AlertNotFoundError",
    "AlertRow",
    "AlertSeverity",
    "ChannelLiteral",
    "ChannelName",
    "DeliveryReport",
    "DeliveryResult",
    "DeliveryStatus",
    "EmailIdentity",
    "OutboundMessage",
    "WebhookPusherError",
    # functions
    "dispatch_alert",
    "plan_alert_dispatch",
    "post_outbound_message",
]
