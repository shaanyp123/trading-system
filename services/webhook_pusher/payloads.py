"""Alert dispatch planner — pure-policy module (backend-spec §3.27 + §1.6 + §2.7).

Same plan-then-apply shape as ``services/risk/state_machine.py``,
``services/risk/sizing.py``, ``services/scheduler/vacation.py``,
``services/scheduler/calendar_import.py``, ``services/qc_adapter/poll.py``,
and ``services/reconciliation/recon.py``: returns an :class:`AlertDispatchPlan`
struct as data; the caller (``services/webhook_pusher/dispatcher.py``) owns
the I/O — POST each :class:`OutboundMessage` via the sender and UPDATE
``alerts.delivery_status`` JSONB.

Why split policy from I/O:

- Anti-pattern A22: unit tests must NOT hit live Discord webhooks or the
  Resend API. Pure policy is trivially testable with zero mocking — tests
  inspect the plan struct.
- The plan struct IS the spec contract — a reviewer can inspect what the
  alert WILL POST without running it.
- Resend/Discord HTTP contract drift (User-Agent gotcha, 429 semantics,
  embed shape changes) lives entirely in the sender. The planner is
  unaffected by platform changes.

Severity routing (backend-spec §1.6 + implementation-guide §3 Week 3 Thu):

- **P0** (kill switch fired, reconciliation break, audit chain break,
  margin auto-trim, broker disconnect): fans out to ``#alerts`` AND
  ``#critical`` AND email backup via Resend.
- **P1** (degraded telemetry, soft cost alert, parameter change proposed):
  ``#alerts`` only.
- **P2** (informational, capacity warnings, parameter change reverted):
  ``#alerts`` only.

Discord embed shape (canonical for both ``#alerts`` and ``#critical``):

- ``title``: the ``alert_category`` enum value (e.g. ``"kill_switch_invoked"``).
- ``description``: the alert's human ``message`` field.
- ``color``: severity → integer (P0 red ``0xFF0000``, P1 orange ``0xFFA500``,
  P2 yellow ``0xFFFF00``).
- ``fields``: top-level keys of the ``detail`` JSONB serialized as
  ``"<key>" -> "<value>"`` strings. Truncated to ``MAX_DETAIL_FIELDS`` to
  avoid Discord's 25-field embed cap (we cap at 10 to leave headroom).
- ``footer.text``: ``"<fired_at_utc ISO> · alert <id>"`` for traceability.

Resend email shape:

- ``from``: ``email_identity.from_address`` (locked: ``shaanrpatel2@gmail.com``
  per `paper.enc.yaml` `resend.from_address`; runtime override via env).
- ``to``: ``[email_identity.to_address]``.
- ``subject``: ``"[<severity> spratcapital] <category>: <truncated message>"``.
- ``text``: plain-text rendering of the alert's category + message + detail
  JSONB (json.dumps with sort_keys=True for determinism).

A27 reminder: this module is pure policy with zero platform calls. The
A27 smoke-test obligation transfers to the dispatcher PR (``dispatcher.py``
+ ``sender.py`` together) — satisfied by ``deploy/webhook_pusher/README.md``
operator runbook in the same commit.

Caller boundary (NOT in this module):

- The Week 4 risk dispatcher (forbidden-whitelist surface) writes the
  alerts row when it transitions ``risk_state``, then calls
  ``dispatcher.dispatch_alert(session, alert_id, ...)`` on a background
  task. This module's :class:`AlertDispatchPlan` is the data the
  dispatcher fans out.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

# ---------------------------------------------------------------------------
# Enums (mirroring schema CHECK constraints + spec §3.27 enum)
# ---------------------------------------------------------------------------


class AlertSeverity(StrEnum):
    """``alerts.severity`` CHECK constraint (alembic 0004:350)."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class AlertCategory(StrEnum):
    """Mirror of the locked Postgres ``alert_category`` enum (backend-spec §3.27).

    New categories REQUIRE both:

    1. An alembic migration adding the value to the ``alert_category`` enum
       (forbidden-whitelist `alembic/**` per dev-guide §11 [A02]).
    2. The same value added here.

    Anti-pattern A04 binds, same way it does for ``audit_log.event_type``
    (see ``services/audit/event_types.py``). Renaming is wire-breaking;
    treat as a schema migration with the same gating bar.

    MOVE_NOTE: backend-spec §4.1.5b plans to define this enum in
    ``services/api/schemas/alerts.py`` once the alerts API surface lands.
    When that module exists, move this definition there and re-export
    here. Today webhook_pusher is the first (and only) consumer, so the
    canonical home is here.
    """

    KILL_SWITCH_INVOKED = "kill_switch_invoked"
    KILL_SWITCH_RESUMED = "kill_switch_resumed"
    HALT_DWELL_WARNING = "halt_dwell_warning"
    AUDIT_WRITE_FAILURE = "audit_write_failure"
    AUDIT_CHAIN_BREAK = "audit_chain_break"
    QC_OBJECTSTORE_DEGRADED = "qc_objectstore_degraded"
    BROKER_DISCONNECT = "broker_disconnect"
    RECONCILIATION_BREAK = "reconciliation_break"
    MARGIN_WARN = "margin_warn"
    MARGIN_AUTO_TRIM = "margin_auto_trim"
    DATA_QUALITY_REJECT = "data_quality_reject"
    DATA_QUALITY_QUARANTINE = "data_quality_quarantine"
    VOL_REGIME_Z_HIGH = "vol_regime_z_high"
    CAPACITY_WARNING = "capacity_warning"
    SLIPPAGE_DRIFT = "slippage_drift"
    MODEL_DECAY = "model_decay"
    CAPITAL_EVENT = "capital_event"
    PARAMETER_CHANGE_PROPOSED = "parameter_change_proposed"
    PARAMETER_CHANGE_REVERTED = "parameter_change_reverted"
    PR_DRAFTED = "pr_drafted"
    PR_REJECTED = "pr_rejected"
    LIVENESS_PROBE_MISSED = "liveness_probe_missed"
    ENGAGEMENT_TIMEOUT = "engagement_timeout"
    COST_ALERT_SOFT = "cost_alert_soft"
    COST_ALERT_HARD = "cost_alert_hard"
    EXTERNAL_WATCHDOG_ALERT = "external_watchdog_alert"
    INCIDENT_REVIEW_REQUIRED = "incident_review_required"
    PHASE_CUTOVER_STARTED = "phase_cutover_started"
    MAINTENANCE_WINDOW = "maintenance_window"
    # PR #154 follow-up (2026-05-16): heartbeat-staleness alerts from
    # ``services.api.heartbeat_probe``. P2-only routing today
    # (#alerts channel only); P0/P1 escalation tied to specific stuck
    # services is a future enhancement.
    HEARTBEAT_STALE = "heartbeat_stale"
    # Recovery-agent follow-up to drill 5 (2026-05-18) + drill 6
    # (2026-05-25), landed 2026-05-26. INSERTed by the task-death hook
    # in ``services.api.async_task_monitor`` when a tracked lifespan
    # task (today: ``order_placement_worker.run_forever``) transitions
    # to ``.done()``. P0-severity routing — fans out to ``#alerts`` +
    # ``#critical`` + email backup. The recovery agent at
    # ``scripts/operator_tools/recovery_agent.py`` polls
    # ``alerts WHERE category = 'worker_failure'`` for unhandled events.
    WORKER_FAILURE = "worker_failure"
    # Exit-pipeline PR-C (2026-05-27). INSERTed by the
    # order_placement_worker's exit branch when the bracket-stop cancel
    # succeeds but the subsequent close-order placement fails. The
    # position is NAKED (no protective stop) — worst failure mode in
    # the exit pipeline (R3 in ``Docs/exit-pipeline-design.md`` §11).
    # P0-severity routing — fans out to ``#alerts`` + ``#critical`` +
    # email backup. Paired with audit event type
    # ``AuditEventType.POSITION_UNPROTECTED``. Operator's runbook is
    # ``scripts/operator_tools/replace_protective_stop.py`` (deferred PR).
    POSITION_UNPROTECTED = "position_unprotected"
    # Option C recon-fix follow-up (2026-05-29). INSERTed by
    # ``services.reconciliation.eod_cycle.run_eod_cycle`` when the
    # ``position_source="reqpositions"`` per-cycle reqPositions fetch
    # (real-time TWS, clientId=4) fails terminally and recon falls back
    # to the FlexQuery position list for the cycle. P1-severity routing —
    # ``#alerts`` only (NO #critical, NO email: the degradation is a
    # single-cycle coverage downgrade, not money at risk). Paired with
    # audit event type
    # ``AuditEventType.RECONCILIATION_DATA_SOURCE_DEGRADED``. The recon
    # cycle still runs cash/NAV + position checks on the FlexQuery
    # snapshot, so the operator-facing effect is "same-day-fill false
    # breaks may reappear this cycle" — actionable but not P0.
    RECONCILIATION_DATA_SOURCE_DEGRADED = "reconciliation_data_source_degraded"
    # Silent-failure follow-up (2026-06-08, P1). INSERTed by the
    # ``order_placement_worker`` when an approved signal's IBKR order
    # placement raises ``IbkrPlacementError`` — broker unavailable OR a
    # contract rejection (e.g. the 2026-06-04 /MYM Error-200 wrong-exchange
    # case fixed in #327). PREVIOUSLY SILENT: the worker logged
    # ``order_placement_broker_unavailable`` + retried but fired no alert,
    # so a failed order on an approved signal went unnoticed. P1-severity
    # routing — ``#alerts`` only (no money at risk: the order simply didn't
    # place + the signal stays ``approved`` for the next poll). No paired
    # audit event (no state change — the orders row stays ``pending``).
    ORDER_PLACEMENT_FAILED = "order_placement_failed"


class ChannelName(StrEnum):
    """Outbound delivery channel.

    The string values double as ``alerts.delivery_status`` JSONB keys —
    the dispatcher writes ``{"discord_alerts": "ok", "discord_critical":
    "ok", "email": "ok"}`` (or per-channel failure values). Renaming a
    value here is wire-breaking — treat as a schema migration.
    """

    DISCORD_ALERTS = "discord_alerts"
    DISCORD_CRITICAL = "discord_critical"
    EMAIL = "email"


# ---------------------------------------------------------------------------
# Locked constants
# ---------------------------------------------------------------------------

#: Severity → set of channels to fan out to. Locked per backend-spec §1.6 +
#: implementation-guide §3 Week 3 Thu interpretation: every alert hits
#: ``#alerts``; P0 also hits ``#critical`` AND email backup.
SEVERITY_TO_CHANNELS: Final[Mapping[AlertSeverity, frozenset[ChannelName]]] = {
    AlertSeverity.P0: frozenset(
        {ChannelName.DISCORD_ALERTS, ChannelName.DISCORD_CRITICAL, ChannelName.EMAIL}
    ),
    AlertSeverity.P1: frozenset({ChannelName.DISCORD_ALERTS}),
    AlertSeverity.P2: frozenset({ChannelName.DISCORD_ALERTS}),
}

#: Severity → Discord embed color (decimal RGB; Discord's API takes int, not hex).
SEVERITY_TO_DISCORD_COLOR: Final[Mapping[AlertSeverity, int]] = {
    AlertSeverity.P0: 0xFF0000,  # red
    AlertSeverity.P1: 0xFFA500,  # orange
    AlertSeverity.P2: 0xFFFF00,  # yellow
}

#: Discord allows up to 25 embed fields; we cap at 10 to keep messages
#: readable and leave headroom if the spec adds more.
MAX_DETAIL_FIELDS: Final[int] = 10

#: Discord field names cap at 256 chars; values cap at 1024. We pre-truncate
#: locally so the API never rejects a payload it could otherwise accept.
DISCORD_FIELD_NAME_MAX: Final[int] = 256
DISCORD_FIELD_VALUE_MAX: Final[int] = 1024

#: Email subject prefix character budget (typical mail client cuts ~70 chars).
EMAIL_SUBJECT_MESSAGE_MAX: Final[int] = 100

#: Resend HTTP API endpoint (locked: ``Resend`` is the email provider per
#: dev-guide §1.5 — NOT SES, NOT SendGrid). Used as ``OutboundMessage.url``
#: when ``ChannelName.EMAIL`` is in the route set.
RESEND_API_URL: Final[str] = "https://api.resend.com/emails"


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertRow:
    """The subset of ``alerts`` columns the planner needs.

    Caller (dispatcher) reads from ``alerts`` table and constructs this.
    Columns not relevant to dispatch (``acknowledged``, ``acknowledged_at_utc``,
    ``resolved_at_utc``, ``triggering_audit_event_uuid``) are deliberately
    omitted to keep the planner's input minimal + testable.

    ``fired_at_utc`` MUST be timezone-aware (anti-pattern A06). Naive
    inputs raise :class:`WebhookPusherError` at plan time.
    """

    id: UUID
    severity: AlertSeverity
    category: AlertCategory
    message: str
    detail: Mapping[str, Any] | None
    fired_at_utc: datetime


@dataclass(frozen=True, slots=True)
class EmailIdentity:
    """Resend identity needed to build the email payload + auth header.

    All three fields source from ``secrets/paper.enc.yaml``:

    - ``from_address`` — sops ``resend.from_address`` (locked:
      ``shaanrpatel2@gmail.com`` per identifiers memory).
    - ``to_address`` — operator email (same value on Phase 0; Phase 1
      may add a second recipient).
    - ``resend_api_key`` — sops ``resend.api_key``. Used to build the
      ``Authorization: Bearer <key>`` header on the OutboundMessage.

    The planner reads these fields but does NOT log them. The frozen
    dataclass + ``__repr__`` redaction would be defense-in-depth; for now
    the API key is treated as caller-managed (loaded from sops at process
    start).
    """

    from_address: str
    to_address: str
    resend_api_key: str


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """One HTTP POST the sender will make.

    ``channel`` identifies the per-channel slot in
    ``alerts.delivery_status`` JSONB the dispatcher updates with the
    sender's :class:`DeliveryResult`.

    ``url`` is the full POST target — Discord webhook URL (auth in path)
    or :data:`RESEND_API_URL` (auth in header).

    ``payload`` is the JSON-serializable body. The sender uses
    ``json=payload`` so httpx handles serialization + Content-Type.

    ``auth_header`` is the full ``Authorization`` header value (e.g.
    ``"Bearer re_test_xxx"``) or ``None`` for Discord (URL-embedded auth).
    """

    channel: ChannelName
    url: str
    payload: Mapping[str, Any]
    auth_header: str | None


@dataclass(frozen=True, slots=True)
class AlertDispatchPlan:
    """The result of planning one alert's dispatch.

    Caller (dispatcher):

    1. ``for msg in plan.outbound_messages: result = await sender.post_outbound_message(client, msg)``
    2. Aggregate ``DeliveryResult`` per channel into a JSONB blob and
       UPDATE ``alerts.delivery_status``.

    ``channels`` is the canonical sorted tuple of channels (matching
    ``outbound_messages``); the dispatcher uses it to pre-populate
    ``delivery_status`` keys so the JSONB shape is deterministic across
    success and failure paths.
    """

    alert_id: UUID
    severity: AlertSeverity
    channels: tuple[ChannelName, ...]
    outbound_messages: tuple[OutboundMessage, ...]


class WebhookPusherError(ValueError):
    """Raised on caller bugs (missing webhook URL, naive datetime, missing
    email_identity when EMAIL is routed).

    ``ValueError``-subclass so FastAPI handlers can trap with one branch
    and return the canonical 400 envelope (dev-guide §3.6).
    """


# ---------------------------------------------------------------------------
# Plan: alert dispatch
# ---------------------------------------------------------------------------

# Locked-routing literal for delivery_status JSONB consumers (the dispatcher
# pre-populates these keys before the sender returns).
ChannelLiteral = Literal["discord_alerts", "discord_critical", "email"]


def plan_alert_dispatch(
    *,
    alert: AlertRow,
    webhook_urls: Mapping[ChannelName, str],
    email_identity: EmailIdentity | None = None,
) -> AlertDispatchPlan:
    """Compute the alert dispatch plan for one alert row.

    Pure function — no I/O. Caller (``dispatcher.dispatch_alert``) POSTs
    each :class:`OutboundMessage` via the sender and updates
    ``alerts.delivery_status``.

    Parameters
    ----------
    alert:
        The alert row's planner-relevant subset. ``fired_at_utc`` must be
        timezone-aware (A06).
    webhook_urls:
        Mapping from channel to URL. Keys MUST cover every channel in the
        severity-routing set or :class:`WebhookPusherError` is raised.
        For Discord channels: the full webhook URL (sops
        ``discord.webhook_urls.alerts`` / ``.critical``). For EMAIL: the
        Resend API URL constant :data:`RESEND_API_URL`.
    email_identity:
        Required IFF the severity routes to email (P0). For P1/P2 may be
        None. Passing ``None`` when EMAIL is in the route set raises
        :class:`WebhookPusherError`.

    Returns
    -------
    AlertDispatchPlan
        See class docstring for the caller contract.

    Raises
    ------
    WebhookPusherError
        On any of: tz-naive ``fired_at_utc``; missing webhook URL for a
        routed channel; EMAIL routed but no ``email_identity``; severity
        outside the locked routing table (defensive, currently
        unreachable as ``AlertSeverity`` is closed).
    """
    if alert.fired_at_utc.tzinfo is None:
        raise WebhookPusherError("alert.fired_at_utc must be timezone-aware (anti-pattern A06)")

    routed = SEVERITY_TO_CHANNELS.get(alert.severity)
    if routed is None:
        # Defensive: AlertSeverity is closed, so this is unreachable in
        # well-typed callers. Raised explicitly so a future severity
        # added without a routing entry fails loudly at plan time.
        raise WebhookPusherError(f"no routing configured for severity {alert.severity!r}")

    missing_urls = [c for c in routed if c not in webhook_urls]
    if missing_urls:
        raise WebhookPusherError(
            "webhook_urls missing entries for routed channels: "
            + ", ".join(sorted(c.value for c in missing_urls))
        )

    if ChannelName.EMAIL in routed and email_identity is None:
        raise WebhookPusherError(
            "email_identity required when severity routes to EMAIL "
            f"(severity={alert.severity.value})"
        )

    # Build outbound messages in canonical sorted order so two plans for
    # the same alert produce identical message tuples (test determinism +
    # predictable delivery_status JSONB key ordering).
    messages: list[OutboundMessage] = []
    for channel in sorted(routed, key=lambda c: c.value):
        if channel is ChannelName.DISCORD_ALERTS or channel is ChannelName.DISCORD_CRITICAL:
            messages.append(
                OutboundMessage(
                    channel=channel,
                    url=webhook_urls[channel],
                    payload=_build_discord_payload(alert),
                    auth_header=None,
                )
            )
        elif channel is ChannelName.EMAIL:
            assert email_identity is not None  # narrowed above
            messages.append(
                OutboundMessage(
                    channel=channel,
                    url=webhook_urls[channel],
                    payload=_build_resend_payload(alert, identity=email_identity),
                    auth_header=f"Bearer {email_identity.resend_api_key}",
                )
            )

    return AlertDispatchPlan(
        alert_id=alert.id,
        severity=alert.severity,
        channels=tuple(m.channel for m in messages),
        outbound_messages=tuple(messages),
    )


# ---------------------------------------------------------------------------
# Internal helpers — payload builders
# ---------------------------------------------------------------------------


def _build_discord_payload(alert: AlertRow) -> Mapping[str, Any]:
    """Build the Discord webhook JSON payload (single embed).

    Discord webhooks accept ``content`` (top-level message text) +
    ``embeds`` (rich embed list). We use a single embed so the alert
    appears as a colored card and the channel reads like a feed of
    severity-coded events.
    """
    color = SEVERITY_TO_DISCORD_COLOR[alert.severity]
    embed: dict[str, Any] = {
        "title": alert.category.value,
        "description": alert.message,
        "color": color,
        "footer": {
            "text": f"{alert.fired_at_utc.isoformat()} · alert {alert.id}",
        },
    }

    if alert.detail:
        embed["fields"] = _build_discord_fields(alert.detail)

    return {
        "username": f"trading-system [{alert.severity.value}]",
        "embeds": [embed],
    }


def _build_discord_fields(detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Convert detail JSONB top-level keys to Discord embed fields.

    Each top-level key becomes one ``{name, value, inline: false}`` field.
    Values that are dicts/lists are JSON-serialized; scalars are str()-ed.
    Both name and value are truncated to Discord's per-field caps with
    a single trailing ellipsis to signal truncation in the UI.
    """
    fields: list[Mapping[str, Any]] = []
    # Sorted for determinism — tests assert exact embed shape.
    for key in sorted(detail.keys())[:MAX_DETAIL_FIELDS]:
        value = detail[key]
        if isinstance(value, dict | list | tuple):
            rendered = json.dumps(value, sort_keys=True, default=str)
        else:
            rendered = str(value)

        fields.append(
            {
                "name": _truncate(key, DISCORD_FIELD_NAME_MAX),
                "value": _truncate(rendered, DISCORD_FIELD_VALUE_MAX),
                "inline": False,
            }
        )
    return fields


def _build_resend_payload(alert: AlertRow, *, identity: EmailIdentity) -> Mapping[str, Any]:
    """Build the Resend ``POST /emails`` JSON payload.

    Resend's contract (https://resend.com/docs/api-reference/emails/send-email):
    ``{"from": "...", "to": ["..."], "subject": "...", "text": "...", "html"?}``.

    We send ``text`` only — plain-text rendering is robust to client
    differences and easier to grep in operator email triage. ``html`` can
    be added later if mobile rendering becomes an issue.
    """
    truncated_message = _truncate(alert.message, EMAIL_SUBJECT_MESSAGE_MAX)
    subject = f"[{alert.severity.value} spratcapital] {alert.category.value}: {truncated_message}"

    body_lines = [
        f"Severity: {alert.severity.value}",
        f"Category: {alert.category.value}",
        f"Fired at: {alert.fired_at_utc.isoformat()}",
        f"Alert id: {alert.id}",
        "",
        "Message:",
        alert.message,
    ]
    if alert.detail:
        body_lines.append("")
        body_lines.append("Detail:")
        body_lines.append(json.dumps(dict(alert.detail), sort_keys=True, indent=2, default=str))

    return {
        "from": identity.from_address,
        "to": [identity.to_address],
        "subject": subject,
        "text": "\n".join(body_lines),
    }


def _truncate(value: str, limit: int) -> str:
    """Truncate ``value`` to ``limit`` chars with a single-char ellipsis suffix.

    The ellipsis costs one char from the limit; the result is always
    ``<= limit`` chars. We use the single-codepoint U+2026 ellipsis so
    one truncated field doesn't push the embed past Discord's per-payload
    byte budget (~6000 chars across all embeds).
    """
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"
    return value[: limit - 1] + "…"


__all__ = [
    "DISCORD_FIELD_NAME_MAX",
    "DISCORD_FIELD_VALUE_MAX",
    "EMAIL_SUBJECT_MESSAGE_MAX",
    "MAX_DETAIL_FIELDS",
    "RESEND_API_URL",
    "SEVERITY_TO_CHANNELS",
    "SEVERITY_TO_DISCORD_COLOR",
    "AlertCategory",
    "AlertDispatchPlan",
    "AlertRow",
    "AlertSeverity",
    "ChannelLiteral",
    "ChannelName",
    "EmailIdentity",
    "OutboundMessage",
    "WebhookPusherError",
    "plan_alert_dispatch",
]
